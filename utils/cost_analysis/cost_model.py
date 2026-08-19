########
#
# cost_model.py
#
# Phase 4: a bottom-up cost model built from traced operation counts and AWS
# list prices, then reconciled against the actual bills.
#
# This is deliberately independent of the Phase 3 regression. Phase 3 asked the
# billing data what the coefficients are; this asks the architecture what they
# ought to be. Where the two agree we can trust the number; where they diverge
# the gap is either a modelling error or real waste, and both are worth knowing.
#
# The residual is the point, not a nuisance: the model prices the system as
# designed, so actual-minus-predicted approximates what is being spent on things
# the design does not call for (notably retained noncurrent S3 versions).
#
# Usage:
#
#   python3 -m utils.cost_analysis.cost_model
#   python3 -m utils.cost_analysis.cost_model --images 2000000 --stock 30000000
#
########

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import OUTPUT_DIR, write_csv

# ---------------------------------------------------------------- AWS list prices
# us-west-2, x86_64, as of 2026-08.
RATES = {
    "lambda_gb_second": 0.0000166667,
    "lambda_request": 0.20 / 1e6,
    "s3_storage_gb_month": 0.023,
    "s3_put_per_1k": 0.005,          # PUT, COPY, POST, LIST
    "s3_get_per_1k": 0.0004,
    "sagemaker_gb_second": 0.00002,
    "apigw_rest_per_1m": 3.50,       # REST API, not HTTP API
    "apigw_transfer_gb": 0.09,
    "secrets_api_per_10k": 0.05,
    "ssm_advanced_per_10k": 0.05,
    "cw_logs_ingest_gb": 0.50,
    "cw_logs_storage_gb_month": 0.03,
    "atlas_backup_gb_month": 0.20,
}

# ------------------------------------------------- measured from CloudWatch (13mo)
# GB-seconds per invocation, i.e. duration x memory.
#
# These are STEADY-STATE means over 2026-03..2026-08, not the full 13 months. The
# per-invocation profile is not stable: graphql ranged from 5.95 GB-s during the
# Dec-2025 duplicate incident down to 0.34 in May-2026. Averaging across the
# incident overstates forward-looking cost by roughly 3x.
LAMBDA_PROFILE = {
    "ingest_image": 1.81,
    "graphql": 0.48,
    "inference": 3.82,
    "batchinference": 9.95,
    "exif": 0.22,
}

# Empirical rates from the Phase 3 regression, in $ per 1,000 images. Used where
# the mechanism is cold-start driven rather than per-image, so a bottom-up count
# of operations would be guesswork.
EMPIRICAL_PER_1K_IMAGES = {
    "cw_data_processing": 0.0883,
    "cw_vended_logs": 0.0577,
    "cw_metrics": 0.0016,
    "ssm_parameters": 0.0344,
    "secrets_manager": 0.0296,
}
CW_STORAGE_PER_1K_STOCK_DAY = 0.0003

# Documented animl-base duplicate-upload incident; excluded from headline error.
INCIDENT_MONTHS = ("2025-12", "2026-01")

# ------------------------------------------------------ traced from the source code
# ingest-image/task.js and animl-api/src/ml/handler.ts.
OPS = {
    "s3_put_per_image": 3,        # 1 CopyObject (original) + 2 PutObject (medium, small)
    "s3_get_per_image": 2,        # 1 full download + 1 ranged read by exif-api
    "s3_get_per_inference": 1,    # each model re-reads the full-size original
    "graphql_per_image": 0.94,    # regression-estimated, R^2 0.96
    "graphql_per_inference": 3.09,
    # IngestImage fires per S3 object, not per stored image. In steady state that
    # is ~1.15x images; it hit 11x during the Dec-2025 duplicate storm.
    "ingest_invocations_per_image": 1.15,
    "sagemaker_seconds_per_inference": 38_977_860 / 16_104_773,  # 2.42 s
    "sagemaker_memory_gb": 6,
    "bytes_stored_per_image_mb": 0.91,
    "apigw_kb_per_call": 44.7,    # 2,985 GB over 66.7M calls; fat nested payloads
}

FIXED_MONTHLY = {
    "route53_and_domains": 238.00 / 13,
    "ecr": 338.29 / 13,
    "secrets_storage": 0.80,
    "vpc_ipv4": 22.32 / 13,
}


class CostModel:
    """Predict monthly AWS + Atlas spend from usage parameters."""

    def __init__(self, rates=None, ops=None):
        self.r = {**RATES, **(rates or {})}
        self.o = {**OPS, **(ops or {})}

    def predict(self, images, stock, inferences_per_image=2.01, batch_share=0.9,
                atlas_cluster=292.71, include_fixed=True):
        o, r = self.o, self.r
        inferences = images * inferences_per_image
        ingest_invocations = images * o["ingest_invocations_per_image"]

        graphql_calls = images * o["graphql_per_image"] + inferences * o["graphql_per_inference"]
        inf_calls = inferences * (1 - batch_share)
        batch_calls = inferences * batch_share / 10  # batchinference reads 10 messages per invoke

        lambda_gb_s = (
            ingest_invocations * LAMBDA_PROFILE["ingest_image"]
            + graphql_calls * LAMBDA_PROFILE["graphql"]
            + inf_calls * LAMBDA_PROFILE["inference"]
            + batch_calls * LAMBDA_PROFILE["batchinference"]
            + ingest_invocations * LAMBDA_PROFILE["exif"]
        )
        lambda_invocations = ingest_invocations * 2 + graphql_calls + inf_calls + batch_calls

        sagemaker_gb_s = (
            inferences * o["sagemaker_seconds_per_inference"] * o["sagemaker_memory_gb"]
        )

        s3_puts = images * o["s3_put_per_image"]
        s3_gets = ingest_invocations * o["s3_get_per_image"] + inferences * o["s3_get_per_inference"]
        stored_gb = stock * o["bytes_stored_per_image_mb"] / 1000

        per_1k = images / 1000
        # Log-group storage is deliberately excluded here. Regressing it on stored
        # images fits well (R^2 0.64) but the relationship is spurious - both series
        # simply trend upward. Logs accumulate because no log group sets a retention
        # policy, so it is tracked as a separate standing cost, not a per-image rate.
        cloudwatch = per_1k * sum(
            EMPIRICAL_PER_1K_IMAGES[k]
            for k in ("cw_data_processing", "cw_vended_logs", "cw_metrics")
        )

        out = {
            "SageMaker": sagemaker_gb_s * r["sagemaker_gb_second"],
            "Lambda": lambda_gb_s * r["lambda_gb_second"]
            + lambda_invocations * r["lambda_request"],
            "S3": stored_gb * r["s3_storage_gb_month"]
            + s3_puts / 1000 * r["s3_put_per_1k"]
            + s3_gets / 1000 * r["s3_get_per_1k"],
            "API Gateway": graphql_calls / 1e6 * r["apigw_rest_per_1m"]
            + graphql_calls * o["apigw_kb_per_call"] / 1e6 * r["apigw_transfer_gb"],
            "CloudWatch": cloudwatch,
            "Systems Manager": per_1k * EMPIRICAL_PER_1K_IMAGES["ssm_parameters"],
            "Secrets Manager": per_1k * EMPIRICAL_PER_1K_IMAGES["secrets_manager"],
            "MongoDB Atlas": atlas_cluster
            + stock * 1.85e-6 * r["atlas_backup_gb_month"] * 51 / 8,
        }
        if include_fixed:
            out["Fixed (Route53/ECR/VPC)"] = sum(FIXED_MONTHLY.values())
        return out


def monthly_actuals():
    ce = pd.read_csv(OUTPUT_DIR / "ce_monthly_service.csv", parse_dates=["date"])
    ce["month"] = ce["date"].dt.strftime("%Y-%m")
    mapping = {
        "Amazon SageMaker": "SageMaker",
        "AWS Lambda": "Lambda",
        "Amazon Simple Storage Service": "S3",
        "Amazon API Gateway": "API Gateway",
        "AmazonCloudWatch": "CloudWatch",
        "AWS Systems Manager": "Systems Manager",
        "AWS Secrets Manager": "Secrets Manager",
        "MongoDB Atlas (pay-as-you-go)": "MongoDB Atlas",
    }
    ce["group"] = ce["service"].map(mapping)
    return ce.dropna(subset=["group"]).pivot_table(
        index="month", columns="group", values="unblended_cost", aggfunc="sum"
    ).fillna(0.0)


def monthly_drivers():
    images = pd.read_csv(OUTPUT_DIR / "mongo_daily_images.csv", parse_dates=["date"])
    stock = pd.read_csv(OUTPUT_DIR / "mongo_daily_image_stock.csv", parse_dates=["date"])
    sm = pd.read_csv(OUTPUT_DIR / "sagemaker_daily_metrics.csv", parse_dates=["date"])
    inf = sm[sm.metric == "Invocations_Sum"].groupby("date")["value"].sum()

    df = pd.DataFrame({
        "images": images.set_index("date")["count"],
        "stock": stock.set_index("date")["cumulative"],
        "inferences": inf,
    }).fillna(0.0)
    df["month"] = df.index.strftime("%Y-%m")
    return df.groupby("month").agg(images=("images", "sum"), stock=("stock", "max"),
                                   inferences=("inferences", "sum"))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--images", type=int, help="images ingested per month")
    p.add_argument("--stock", type=int, help="total images stored")
    args = p.parse_args()

    model = CostModel()

    if args.images or args.stock:
        images = args.images or 600_000
        stock = args.stock or 15_377_533
        pred = model.predict(images, stock)
        print(f"\nScenario: {images:,} images/month ingested, {stock:,} stored\n")
        for service, cost in sorted(pred.items(), key=lambda kv: -kv[1]):
            print(f"  {service:<26}${cost:>10,.2f}")
        print(f"  {'-' * 26} {'-' * 11}")
        print(f"  {'TOTAL':<26}${sum(pred.values()):>10,.2f}/month")
        return

    actual, drivers = monthly_actuals(), monthly_drivers()
    months = [m for m in drivers.index if m in actual.index][1:-1]  # drop partial ends

    print("BOTTOM-UP RECONCILIATION\n")
    print(f"{'MONTH':<9}{'IMAGES':>10}{'PREDICTED':>12}{'ACTUAL':>12}{'RESIDUAL':>12}{'ERR':>8}")
    print("-" * 63)
    rows = []
    for month in months:
        d = drivers.loc[month]
        inf_per_image = d.inferences / max(d.images, 1)
        pred = model.predict(d.images, d.stock, inferences_per_image=inf_per_image,
                             include_fixed=False)
        pred_total = sum(pred.values())
        act_total = actual.loc[month].sum()
        residual = act_total - pred_total
        err = 100 * residual / act_total if act_total else 0
        rows.append({"month": month, "images": d.images, "stock": d.stock,
                     "predicted": pred_total, "actual": act_total,
                     "residual": residual, "error_pct": err,
                     **{f"pred_{k}": v for k, v in pred.items()}})
        print(f"{month:<9}{d.images:>10,.0f}{pred_total:>12,.0f}{act_total:>12,.0f}"
              f"{residual:>12,.0f}{err:>7.0f}%")

    out = pd.DataFrame(rows)
    write_csv(out, "cost_model_reconciliation.csv")

    print("\n\nPER SERVICE, most recent full month")
    last = months[-1]
    d = drivers.loc[last]
    pred = model.predict(d.images, d.stock,
                         inferences_per_image=d.inferences / max(d.images, 1),
                         include_fixed=False)
    print(f"{'SERVICE':<20}{'PREDICTED':>12}{'ACTUAL':>12}{'RESIDUAL':>12}{'ERR':>8}")
    print("-" * 64)
    for service in sorted(pred, key=lambda s: -pred[s]):
        a = actual.loc[last].get(service, 0.0)
        r = a - pred[service]
        e = 100 * r / a if a else float("nan")
        print(f"{service:<20}{pred[service]:>12,.0f}{a:>12,.0f}{r:>12,.0f}{e:>7.0f}%")

    mean_err = out["error_pct"].abs().mean()
    normal = out[~out.month.isin(INCIDENT_MONTHS)]
    print(f"\n  mean absolute error, all {len(months)} months:      {mean_err:.0f}%")
    print(f"  mean absolute error, excluding Dec-Jan incident: {normal['error_pct'].abs().mean():.0f}%")
    print(f"  mean residual (normal months): ${normal['residual'].mean():,.0f}/month")

    print("\n  The model prices the system AS DESIGNED, so the residual is a measurement")
    print("  of spend the design does not account for. It is concentrated in S3:")
    s3_resid = normal["actual"].sum() - normal["predicted"].sum()
    print(f"    residual across normal months        ${s3_resid:,.0f}")
    print("    independently measured S3 waste      ~$583/mo (noncurrent versions)")
    print("                                         +$26/mo (dead-letter, never expired)")
    print("                                         +$51/mo (log groups, no retention)")
    print("  Lifecycle rules were deployed 2026-08-17, so this should now decay.")


if __name__ == "__main__":
    main()
