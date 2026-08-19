########
#
# fetch_mongo_usage.py
#
# Phase 2 of the Animl AWS cost analysis: pull the usage denominators that the
# cost data gets divided by.
#
# Cost Explorer tells us what we spent; these are the units of work that spending
# bought. Together they give unit economics: cost per image ingested, per image
# stored per month, per inference, per failed upload.
#
# Note the distinction between three counts that are easy to conflate:
#   imageattempts - every ingestion ATTEMPT (what drives ingest cost)
#   images        - attempts that produced a stored record (what drives retention cost)
#   imageerrors   - attempts that failed, largely duplicates (cost with no value)
#
# READ-ONLY. Runs aggregations against the prod cluster; these are collection
# scans over millions of documents, so expect this to take a few minutes and
# prefer running it against a secondary.
#
# Usage:
#
#   python3 -m utils.cost_analysis.fetch_mongo_usage --months 13
#
########

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import REPO_ROOT, write_csv

DAY_FMT = "%Y-%m-%d"


def connect():
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    url = os.environ.get("MONGODB_URL")
    if not url:
        print(
            "MONGODB_URL is not set. Add it to .env at the repo root:\n"
            "  MONGODB_URL=mongodb+srv://<user>:<password>@<host>/<database>",
            file=sys.stderr,
        )
        sys.exit(1)

    from pymongo import MongoClient, ReadPreference

    client = MongoClient(url, serverSelectionTimeoutMS=15000)
    # Analytics scans should not compete with the API for the primary.
    return client.get_database().with_options(read_preference=ReadPreference.SECONDARY_PREFERRED)


def daily_counts(db, collection, date_field, start, extra_group=None, label=None):
    """Count documents per day, optionally split by a second field."""
    group_id = {"day": {"$dateToString": {"format": DAY_FMT, "date": f"${date_field}"}}}
    if extra_group:
        group_id[extra_group] = f"${extra_group}"

    pipeline = [
        {"$match": {date_field: {"$gte": start}}},
        {"$group": {"_id": group_id, "n": {"$sum": 1}}},
        {"$sort": {"_id.day": 1}},
    ]

    rows = []
    for doc in db[collection].aggregate(pipeline, allowDiskUse=True):
        row = {"date": doc["_id"]["day"], "count": doc["n"]}
        if extra_group:
            row[extra_group] = doc["_id"].get(extra_group)
        rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date")
    print(f"  {label or collection}: {len(df):,} rows, {df['count'].sum():,.0f} documents")
    return df


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--months", type=int, default=13)
    p.add_argument("--top-projects", type=int, default=20)
    args = p.parse_args()

    start = datetime.now(timezone.utc) - timedelta(days=args.months * 31)
    start = start.replace(tzinfo=None)
    db = connect()
    print(f"MongoDB usage pull from {start.date()} (db: {db.name})\n")

    print("[1/6] daily images created (ingestion flow)")
    images = daily_counts(db, "images", "dateAdded", start, label="images")
    write_csv(images, "mongo_daily_images.csv")

    print("[2/6] daily ingestion attempts")
    attempts = daily_counts(db, "imageattempts", "created", start, label="imageattempts")
    write_csv(attempts, "mongo_daily_attempts.csv")

    print("[3/6] daily ingestion errors by type")
    errors = daily_counts(db, "imageerrors", "created", start, extra_group="error", label="imageerrors")
    write_csv(errors, "mongo_daily_errors.csv")

    print("[4/6] cumulative image stock (retention driver)")
    total = db["images"].estimated_document_count()
    if not images.empty:
        stock = images.groupby("date")["count"].sum().sort_index()
        # Work backwards from today's total so the series is anchored to reality.
        cumulative = total - stock[::-1].cumsum()[::-1] + stock
        stock_df = pd.DataFrame({"date": stock.index, "added": stock.values, "cumulative": cumulative.values})
        write_csv(stock_df, "mongo_daily_image_stock.csv")

    print("[5/6] images per project")
    proj = pd.DataFrame(
        db["images"].aggregate(
            [{"$group": {"_id": "$projectId", "n": {"$sum": 1}}}, {"$sort": {"n": -1}}],
            allowDiskUse=True,
        )
    ).rename(columns={"_id": "project_id", "n": "images"})
    write_csv(proj, "mongo_images_by_project.csv")

    print("[6/6] batches per month")
    batches = daily_counts(db, "batches", "processingStart", start, label="batches")
    write_csv(batches, "mongo_daily_batches.csv")

    # ---- summary ----
    print("\n" + "=" * 78)
    n_img, n_att = images["count"].sum(), attempts["count"].sum()
    n_err = errors["count"].sum()
    print(f"  images created in window   : {n_img:>12,.0f}")
    print(f"  ingestion attempts         : {n_att:>12,.0f}")
    print(f"  ingestion errors           : {n_err:>12,.0f}"
          f"   ({100 * n_err / max(n_att, 1):.1f}% of attempts)")
    print(f"  total images stored        : {total:>12,.0f}")

    if not errors.empty:
        print("\n  Errors by type:")
        by_type = errors.groupby("error")["count"].sum().sort_values(ascending=False)
        for err, n in by_type.head(12).items():
            print(f"    {n:>12,.0f}  {100 * n / n_err:>5.1f}%  {str(err)[:60]}")

    if not proj.empty:
        print(f"\n  Top {args.top_projects} projects by stored images "
              f"({len(proj):,} projects total):")
        for _, r in proj.head(args.top_projects).iterrows():
            print(f"    {r['images']:>12,.0f}  {100 * r['images'] / proj['images'].sum():>5.1f}%  {r['project_id']}")
        top10 = proj.head(10)["images"].sum()
        print(f"\n  Top 10 projects hold {100 * top10 / proj['images'].sum():.1f}% of all images")


if __name__ == "__main__":
    main()
