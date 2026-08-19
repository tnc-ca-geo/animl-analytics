########
#
# unit_economics.py
#
# Phase 2 synthesis: divide the Cost Explorer spend by the usage denominators to
# get per-image, per-inference and per-image-month unit costs.
#
# Reads only the CSVs already written by the other scripts; makes no AWS or
# MongoDB calls.
#
# Usage:
#
#   python3 -m utils.cost_analysis.unit_economics
#
########

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import OUTPUT_DIR

# 13-month actuals from Cost Explorer, by usage type.
COST = {
    "sagemaker": 4681.81,
    "lambda_ingest": 807.00,
    "lambda_graphql": 1929.85,
    "lambda_inference": 234.94 + 162.45,
    "lambda_exif": 60.06,
    "s3_requests": 241.83 + 24.41,
    "s3_storage": 6839.61,
    "apigw": 480.09,
    "cloudwatch": 2682.29,
    "mongodb": 5295.00,
}
SAGEMAKER_INVOCATIONS = 16_104_773
INGESTIMAGE_INVOCATIONS = 15_121_303
SERVING_GB = 14_005.2
STORED_IMAGES = 15_377_533
S3_GB_MONTH = 0.023
INDEXES_ON_IMAGES = 8  # a duplicate raises one E11000 error document per index


def load(name, **kw):
    return pd.read_csv(OUTPUT_DIR / name, **kw)


def normalise_error(text):
    text = str(text)
    if "E11000" in text:
        return "DUPLICATE_IMAGE (E11000)"
    return text[:52] + "..." if len(text) > 55 else text


def main():
    err = load("mongo_daily_errors.csv", parse_dates=["date"])
    img = load("mongo_daily_images.csv", parse_dates=["date"])
    att = load("mongo_daily_attempts.csv", parse_dates=["date"])
    proj = load("mongo_images_by_project.csv")

    err["category"] = err["error"].map(normalise_error)
    total_err = err["count"].sum()

    print(f"IMAGE ERRORS — 13 months: {total_err:,.0f} documents\n")
    print(f"{'CATEGORY':<56}{'DOCS':>12}{'%':>8}")
    print("-" * 76)
    by_cat = err.groupby("category")["count"].sum().sort_values(ascending=False)
    for cat, n in by_cat.head(10).items():
        print(f"{cat:<56}{n:>12,.0f}{100 * n / total_err:>7.1f}%")

    dupes = by_cat.get("DUPLICATE_IMAGE (E11000)", 0)
    distinct_dupes = dupes / INDEXES_ON_IMAGES
    print("-" * 76)
    print(f"{'duplicate error documents':<56}{dupes:>12,.0f}{100 * dupes / total_err:>7.1f}%")
    print(f"{'  approx distinct duplicate images (docs / 8 indexes)':<56}{distinct_dupes:>12,.0f}")

    n_img = img["count"].sum()
    n_att = att["count"].sum()

    print("\n" + "=" * 76)
    print("UNIT ECONOMICS — 13 months")
    print("=" * 76)
    print(f"\n  images created                {n_img:>14,.0f}")
    print(f"  ingestion attempts            {n_att:>14,.0f}")
    print(f"  IngestImage invocations       {INGESTIMAGE_INVOCATIONS:>14,.0f}")
    print(f"  SageMaker invocations         {SAGEMAKER_INVOCATIONS:>14,.0f}")

    print(f"\n  inferences per image created  {SAGEMAKER_INVOCATIONS / n_img:>14.2f}")
    print(f"  IngestImage calls per image   {INGESTIMAGE_INVOCATIONS / n_img:>14.2f}")
    wasted = INGESTIMAGE_INVOCATIONS - n_img
    print(f"  IngestImage calls yielding no image: {wasted:>11,.0f}"
          f"  ({100 * wasted / INGESTIMAGE_INVOCATIONS:.0f}%)")

    print("\n  PER IMAGE INGESTED (one-time):")
    ingest_components = [
        ("SageMaker inference", COST["sagemaker"]),
        ("IngestImage Lambda", COST["lambda_ingest"]),
        ("inference + batchinference Lambda", COST["lambda_inference"]),
        ("S3 PUT/GET requests", COST["s3_requests"]),
        ("exif-api Lambda", COST["lambda_exif"]),
    ]
    subtotal = 0.0
    for label, cost in ingest_components:
        print(f"    {label:<40} ${cost / n_img:>10.6f}")
        subtotal += cost
    print(f"    {'-' * 40} {'-' * 11}")
    print(f"    {'subtotal (excludes graphql)':<40} ${subtotal / n_img:>10.6f}")
    print(f"    {'per 1,000 images':<40} ${1000 * subtotal / n_img:>10.3f}")

    mb_per_image = SERVING_GB * 1000 / STORED_IMAGES
    monthly = mb_per_image / 1000 * S3_GB_MONTH
    print("\n  PER IMAGE STORED PER MONTH (recurring):")
    print(f"    {'stored bytes per image':<40} {mb_per_image:>10.2f} MB")
    print(f"    {'S3 serving storage':<40} ${monthly:>10.6f}")
    print(f"    {'per 1,000 images per month':<40} ${1000 * monthly:>10.3f}")
    print(f"    {'per 1,000 images per year':<40} ${12000 * monthly:>10.3f}")

    print(f"\n  BREAK-EVEN: an ingested image costs ${subtotal / n_img:.6f} once, then "
          f"${monthly:.6f}/month.")
    print(f"    Retention overtakes ingestion after "
          f"{(subtotal / n_img) / monthly:,.0f} months "
          f"({(subtotal / n_img) / monthly / 12:,.1f} years).")

    total = proj["images"].sum()
    print(f"\n  TENANT CONCENTRATION: {len(proj)} projects hold images")
    for n in (1, 3, 5, 10, 20):
        share = 100 * proj.head(n)["images"].sum() / total
        print(f"    top {n:>2} projects hold {share:>5.1f}%")

    print("\n  Largest projects, with modelled cost at the rates above:")
    print(f"    {'PROJECT':<44}{'IMAGES':>12}{'INGEST $':>11}{'STORAGE $/MO':>14}")
    for _, r in proj.head(10).iterrows():
        n = r["images"]
        print(f"    {str(r['project_id']):<44}{n:>12,.0f}"
              f"{n * subtotal / n_img:>11,.2f}{n * monthly:>14,.2f}")


if __name__ == "__main__":
    main()
