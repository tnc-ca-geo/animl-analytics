########
#
# fetch_usage_metrics.py
#
# Phase 2 of the Animl AWS cost analysis: pull the usage denominators that the
# Phase 3 regression divides cost by.
#
# Cost Explorer tells us what we spent. These metrics tell us what we spent it
# on: how many inferences ran, how many bytes are stored, how many Lambda
# invocations fired, and how big each model container is. Cost Explorer cannot
# split Lambda cost by function without resource-level CUR, so per-function
# invocation and duration metrics are how we allocate that bill.
#
# READ-ONLY.
#
# Usage:
#
#   python3 utils/cost_analysis/fetch_usage_metrics.py --months 13
#
########

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import AWS_REGION, get_client, get_session, write_csv

DAY = 86400

# CloudWatch serves 1-hour-and-coarser data for 455 days.
MAX_METRIC_DAYS = 450

S3_STORAGE_TYPES = [
    "StandardStorage",
    "IntelligentTieringFAStorage",
    "IntelligentTieringIAStorage",
    "IntelligentTieringAAStorage",
    "IntelligentTieringAIAStorage",
    "IntelligentTieringDAAStorage",
    "StandardIAStorage",
    "OneZoneIAStorage",
    "GlacierInstantRetrievalStorage",
    "GlacierStorage",
    "DeepArchiveStorage",
    "ReducedRedundancyStorage",
]


def safe_id(prefix, index):
    """CloudWatch metric query ids must be unique and start with a lowercase letter."""
    return f"{prefix}{index}"


def run_metric_data(cw, queries, start, end):
    """Execute GetMetricData in chunks, following pagination, into tidy rows."""
    rows = []
    for chunk_start in range(0, len(queries), 100):
        chunk = queries[chunk_start : chunk_start + 100]
        token = None
        while True:
            kwargs = {
                "MetricDataQueries": [q["query"] for q in chunk],
                "StartTime": start,
                "EndTime": end,
                "ScanBy": "TimestampAscending",
            }
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)

            meta = {q["query"]["Id"]: q for q in chunk}
            for result in resp["MetricDataResults"]:
                info = meta.get(result["Id"])
                if not info:
                    continue
                for ts, value in zip(result["Timestamps"], result["Values"]):
                    rows.append({**info["labels"], "date": ts, "value": value})

            token = resp.get("NextToken")
            if not token:
                break
    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"]).dt.tz_localize(None).dt.normalize()
    return df


def fetch_sagemaker(session, start, end):
    """Per-endpoint invocation counts and latency: the inference cost driver."""
    sm = get_client("sagemaker", session=session)
    cw = get_client("cloudwatch", session=session)

    endpoints = []
    paginator = sm.get_paginator("list_endpoints")
    for page in paginator.paginate():
        for ep in page["Endpoints"]:
            detail = sm.describe_endpoint(EndpointName=ep["EndpointName"])
            config_name = detail.get("EndpointConfigName")
            memory, max_concurrency, variant_type = None, None, None
            if config_name:
                try:
                    cfg = sm.describe_endpoint_config(EndpointConfigName=config_name)
                    variants = cfg.get("ProductionVariants") or []
                    if variants:
                        v = variants[0]
                        serverless = v.get("ServerlessConfig") or {}
                        memory = serverless.get("MemorySizeInMB")
                        max_concurrency = serverless.get("MaxConcurrency")
                        # An InstanceType with no ServerlessConfig means an
                        # always-on endpoint billing 24/7.
                        variant_type = v.get("InstanceType") or "serverless"
                except ClientError:
                    pass
            endpoints.append(
                {
                    "endpoint_name": ep["EndpointName"],
                    "status": ep["EndpointStatus"],
                    "created": ep.get("CreationTime"),
                    "config_name": config_name,
                    "memory_mb": memory,
                    "max_concurrency": max_concurrency,
                    "instance_type": variant_type,
                }
            )

    inventory = pd.DataFrame(endpoints)
    write_csv(inventory, "sagemaker_endpoint_inventory.csv")

    always_on = inventory[
        inventory["instance_type"].notna() & (inventory["instance_type"] != "serverless")
    ]
    if not always_on.empty:
        print("\n  !! ALWAYS-ON SageMaker endpoints found (billing 24/7):")
        for _, row in always_on.iterrows():
            print(f"     {row['endpoint_name']}  {row['instance_type']}")
        print()

    queries = []
    for i, name in enumerate(inventory["endpoint_name"].tolist()):
        for stat, metric in (("Sum", "Invocations"), ("Average", "ModelLatency")):
            queries.append(
                {
                    "labels": {"endpoint_name": name, "metric": metric},
                    "query": {
                        "Id": safe_id("sm", len(queries)),
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/SageMaker",
                                "MetricName": metric,
                                "Dimensions": [
                                    {"Name": "EndpointName", "Value": name},
                                    {"Name": "VariantName", "Value": "AllTraffic"},
                                ],
                            },
                            "Period": DAY,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    },
                }
            )

    metrics = run_metric_data(cw, queries, start, end)
    write_csv(metrics, "sagemaker_daily_metrics.csv")
    return inventory, metrics


def fetch_lambda(session, start, end):
    """Per-function invocations and duration: how we allocate the Lambda bill."""
    lam = get_client("lambda", session=session)
    cw = get_client("cloudwatch", session=session)

    functions = []
    paginator = lam.get_paginator("list_functions")
    for page in paginator.paginate():
        for fn in page["Functions"]:
            functions.append(
                {
                    "function_name": fn["FunctionName"],
                    "memory_mb": fn.get("MemorySize"),
                    "timeout_s": fn.get("Timeout"),
                    "runtime": fn.get("Runtime", "container"),
                    "architecture": (fn.get("Architectures") or ["x86_64"])[0],
                    "package_type": fn.get("PackageType"),
                    "code_size_bytes": fn.get("CodeSize"),
                }
            )

    inventory = pd.DataFrame(functions)
    write_csv(inventory, "lambda_inventory.csv")

    queries = []
    for name in inventory["function_name"].tolist():
        for stat, metric in (("Sum", "Invocations"), ("Sum", "Duration"), ("Average", "Duration")):
            queries.append(
                {
                    "labels": {"function_name": name, "metric": metric, "stat": stat},
                    "query": {
                        "Id": safe_id("lam", len(queries)),
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/Lambda",
                                "MetricName": metric,
                                "Dimensions": [{"Name": "FunctionName", "Value": name}],
                            },
                            "Period": DAY,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    },
                }
            )

    metrics = run_metric_data(cw, queries, start, end)
    write_csv(metrics, "lambda_daily_metrics.csv")
    return inventory, metrics


def fetch_s3_storage(session, start, end):
    """Daily bytes and object counts per bucket per storage class.

    This is the independent check on the largest line item: storage cost divided
    by the per-GB-month price should equal these bytes.
    """
    s3 = get_client("s3", session=session)
    cw = get_client("cloudwatch", session=session)

    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]

    queries = []
    for bucket in buckets:
        for storage_type in S3_STORAGE_TYPES:
            queries.append(
                {
                    "labels": {
                        "bucket": bucket,
                        "metric": "BucketSizeBytes",
                        "storage_type": storage_type,
                    },
                    "query": {
                        "Id": safe_id("s3s", len(queries)),
                        "MetricStat": {
                            "Metric": {
                                "Namespace": "AWS/S3",
                                "MetricName": "BucketSizeBytes",
                                "Dimensions": [
                                    {"Name": "BucketName", "Value": bucket},
                                    {"Name": "StorageType", "Value": storage_type},
                                ],
                            },
                            "Period": DAY,
                            "Stat": "Average",
                        },
                        "ReturnData": True,
                    },
                }
            )
        queries.append(
            {
                "labels": {
                    "bucket": bucket,
                    "metric": "NumberOfObjects",
                    "storage_type": "AllStorageTypes",
                },
                "query": {
                    "Id": safe_id("s3n", len(queries)),
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/S3",
                            "MetricName": "NumberOfObjects",
                            "Dimensions": [
                                {"Name": "BucketName", "Value": bucket},
                                {"Name": "StorageType", "Value": "AllStorageTypes"},
                            ],
                        },
                        "Period": DAY,
                        "Stat": "Average",
                    },
                    "ReturnData": True,
                },
            }
        )

    metrics = run_metric_data(cw, queries, start, end)
    write_csv(metrics, "s3_daily_storage.csv")
    return metrics


def fetch_ecr(session):
    """Per-repository image sizes: the 'cost per model offering' denominator.

    Summing image sizes overstates real storage because layers are shared, so
    treat this as an upper bound and a relative ranking across models.
    """
    ecr = get_client("ecr", session=session)

    rows = []
    repo_paginator = ecr.get_paginator("describe_repositories")
    for page in repo_paginator.paginate():
        for repo in page["repositories"]:
            name = repo["repositoryName"]
            image_paginator = ecr.get_paginator("describe_images")
            for img_page in image_paginator.paginate(repositoryName=name):
                for img in img_page["imageDetails"]:
                    rows.append(
                        {
                            "repository": name,
                            "digest": img.get("imageDigest"),
                            "tags": ",".join(img.get("imageTags") or []),
                            "size_bytes": img.get("imageSizeInBytes", 0),
                            "pushed_at": img.get("imagePushedAt"),
                        }
                    )

    images = pd.DataFrame(rows)
    write_csv(images, "ecr_images.csv")

    if not images.empty:
        summary = (
            images.groupby("repository")
            .agg(
                image_count=("digest", "count"),
                untagged_count=("tags", lambda s: (s == "").sum()),
                total_gb=("size_bytes", lambda s: s.sum() / 1e9),
            )
            .sort_values("total_gb", ascending=False)
            .reset_index()
        )
        write_csv(summary, "ecr_repository_summary.csv")
        print("\n  ECR storage by repository (upper bound, shared layers counted once per image):")
        for _, row in summary.iterrows():
            print(
                f"     {row['total_gb']:>7.2f} GB  {row['repository']:<45} "
                f"{row['image_count']:>3} images ({row['untagged_count']} untagged)"
            )
    return images


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--months", type=int, default=13)
    parser.add_argument(
        "--skip",
        default="",
        help="comma-separated sections to skip: sagemaker,lambda,s3,ecr",
    )
    args = parser.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    days = min(args.months * 31, MAX_METRIC_DAYS)
    start = end - timedelta(days=days)

    session = get_session()

    print(f"Usage metric pull: {start.date()} -> {end.date()} (region {AWS_REGION})\n")

    if "sagemaker" not in skip:
        print("[1/4] SageMaker endpoints + invocation metrics")
        fetch_sagemaker(session, start, end)

    if "lambda" not in skip:
        print("[2/4] Lambda functions + invocation/duration metrics")
        fetch_lambda(session, start, end)

    if "s3" not in skip:
        print("[3/4] S3 daily storage bytes + object counts")
        fetch_s3_storage(session, start, end)

    if "ecr" not in skip:
        print("[4/4] ECR repository image sizes")
        fetch_ecr(session)

    print("\nDone.")


if __name__ == "__main__":
    main()
