########
#
# audit_orphaned_uploads.py
#
# Inventories orphaned bulk-upload artifacts left behind in the ingestion bucket.
#
# The ingestion bucket is meant to be transient: IngestImage deletes each image
# after processing it, and the ingest-zip Batch job deletes the .zip after
# extracting it. But in ingest-zip/index.js the zip's DeleteObject is the last
# statement in the try block -- after extracting and PUTting every image at
# concurrency 100. Any failure along the way (a single image PUT throwing, a
# Fargate OOM on a multi-GB zip, a task timeout) jumps to catch{} and the zip is
# never deleted. AWS Batch RetryStrategy.Attempts=1 means there is no second
# attempt and therefore no second chance at cleanup.
#
# Zip keys are `<batchId>.zip`, which joins directly to Batch._id in MongoDB, so
# we can tell a genuinely orphaned upload from one that is still in flight.
#
# THIS SCRIPT IS STRICTLY READ-ONLY. It never deletes anything. With
# --write-manifest it emits a newline-delimited list of candidate keys for a
# human to review and act on separately.
#
# Usage:
#
#   python3 -m utils.cost_analysis.audit_orphaned_uploads
#   python3 -m utils.cost_analysis.audit_orphaned_uploads --deep --write-manifest
#
########

import argparse
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import OUTPUT_DIR, REPO_ROOT, get_client, write_csv

GB = 1_000_000_000
S3_STANDARD_GB_MONTH = 0.023

# A zip younger than this may still be mid-extraction; never flag it.
DEFAULT_MIN_AGE_DAYS = 7


def load_batches(mongodb_url, db_name):
    """Return {batchId: batch_doc} for every Batch record, or None if unavailable."""
    try:
        from pymongo import MongoClient
    except ImportError:
        print("  pymongo not installed; skipping MongoDB join", file=sys.stderr)
        return None

    try:
        client = MongoClient(mongodb_url, serverSelectionTimeoutMS=8000)
        db = client[db_name] if db_name else client.get_database()
        docs = db["batches"].find(
            {},
            {
                "_id": 1,
                "projectId": 1,
                "user": 1,
                "created": 1,
                "originalFile": 1,
                "uploadComplete": 1,
                "processingStart": 1,
                "ingestionComplete": 1,
                "processingEnd": 1,
                "stoppingInitiated": 1,
                "total": 1,
            },
        )
        batches = {d["_id"]: d for d in docs}
        print(f"  loaded {len(batches):,} Batch records from '{db.name}'")
        return batches
    except Exception as e:  # noqa: BLE001 - surface any connection problem plainly
        print(f"  MongoDB unavailable ({type(e).__name__}); continuing without it", file=sys.stderr)
        return None


def list_root_objects(s3, bucket):
    """List only root-level keys. Zips live at the root; extracted images live
    under `batch-<uuid>/` prefixes, so the delimiter keeps this to ~thousands of
    keys instead of walking tens of millions."""
    objects, prefixes = [], []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Delimiter": "/", "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kwargs)
        objects.extend(resp.get("Contents", []))
        prefixes.extend(p["Prefix"] for p in resp.get("CommonPrefixes", []))
        token = resp.get("NextContinuationToken")
        if not token:
            break
        if len(objects) % 5000 == 0 and objects:
            print(f"    ...{len(objects):,} root objects, {len(prefixes):,} prefixes")
    return objects, prefixes


def classify(row, batches, min_age_days):
    """Decide whether a zip is safe to reclaim, and say why."""
    if row["age_days"] < min_age_days:
        return "RECENT_do_not_touch", "younger than the safety threshold"

    if batches is None:
        return "UNKNOWN_no_db", "MongoDB not consulted"

    b = batches.get(row["batch_id"])
    if b is None:
        return "ORPHAN_no_batch_record", "no Batch document exists for this id"
    if b.get("stoppingInitiated"):
        return "ORPHAN_cancelled", "batch was cancelled by the user"
    if b.get("ingestionComplete"):
        # ingest-zip deletes the zip immediately BEFORE setting ingestionComplete,
        # so a surviving zip here means the delete itself did not take effect.
        return "ORPHAN_ingestion_complete", "ingestion completed; zip should already be gone"
    if b.get("processingStart"):
        return "ORPHAN_extraction_failed", "extraction started but never completed"
    if b.get("uploadComplete"):
        return "ORPHAN_never_processed", "upload finished but extraction never started"
    return "ORPHAN_upload_incomplete", "upload never completed"


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default="animl-images-ingestion-prod")
    p.add_argument("--db", default=None, help="Mongo database name (default: from the URI)")
    p.add_argument(
        "--min-age-days",
        type=int,
        default=DEFAULT_MIN_AGE_DAYS,
        help="never flag anything younger than this (default: %(default)s)",
    )
    p.add_argument(
        "--deep",
        action="store_true",
        help="also size the leftover batch-<uuid>/ image prefixes (slow: lists every object)",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=24,
        help="parallel prefix scanners for --deep (default: %(default)s)",
    )
    p.add_argument(
        "--write-manifest",
        action="store_true",
        help="write a reviewable list of candidate keys (still deletes nothing)",
    )
    args = p.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass

    print(f"Auditing s3://{args.bucket} (read-only)\n")

    print("[1/4] loading Batch records")
    mongodb_url = os.environ.get("MONGODB_URL")
    batches = load_batches(mongodb_url, args.db) if mongodb_url else None
    if not mongodb_url:
        print("  MONGODB_URL not set; classification will be age-based only", file=sys.stderr)

    print("[2/4] listing root-level objects")
    s3 = get_client("s3")
    objects, prefixes = list_root_objects(s3, args.bucket)
    print(f"  {len(objects):,} root objects, {len(prefixes):,} batch-* prefixes")

    now = datetime.now(timezone.utc)
    rows = []
    for o in objects:
        key = o["Key"]
        rows.append(
            {
                "key": key,
                "size_bytes": o["Size"],
                "size_gb": o["Size"] / GB,
                "last_modified": o["LastModified"],
                "age_days": (now - o["LastModified"]).days,
                "is_zip": key.lower().endswith(".zip"),
                "batch_id": key[:-4] if key.lower().endswith(".zip") else None,
            }
        )

    df = pd.DataFrame(rows)
    if df.empty:
        print("\nNothing at the root of this bucket.")
        return

    print("[3/4] classifying")
    zips = df[df.is_zip].copy()
    if zips.empty:
        print("  no .zip objects found")
    else:
        verdicts = zips.apply(lambda r: classify(r, batches, args.min_age_days), axis=1)
        zips["status"] = [v[0] for v in verdicts]
        zips["reason"] = [v[1] for v in verdicts]

        for col, src in (
            ("project_id", "projectId"),
            ("uploaded_by", "user"),
            ("original_file", "originalFile"),
            ("image_count", "total"),
        ):
            zips[col] = zips.batch_id.map(
                lambda b: (batches.get(b, {}) or {}).get(src) if batches else None
            )

        zips["annual_cost"] = zips.size_gb * S3_STANDARD_GB_MONTH * 12
        zips = zips.sort_values("size_gb", ascending=False)
        write_csv(zips, "orphaned_zips.csv")

    print("[4/4] summarising\n")

    non_zip = df[~df.is_zip]
    if not non_zip.empty:
        print(
            f"  {len(non_zip):,} non-zip root objects "
            f"({non_zip.size_gb.sum():,.1f} GB) -- stray images, not classified here\n"
        )

    if not zips.empty:
        g = (
            zips.groupby("status")
            .agg(count=("key", "size"), gb=("size_gb", "sum"), annual=("annual_cost", "sum"))
            .sort_values("gb", ascending=False)
        )
        print(f"  {'STATUS':<28}{'COUNT':>8}{'GB':>12}{'$/MONTH':>11}{'$/YEAR':>11}")
        print("  " + "-" * 70)
        for status, r in g.iterrows():
            print(
                f"  {status:<28}{r['count']:>8,.0f}{r.gb:>12,.1f}"
                f"{r.annual / 12:>11,.2f}{r.annual:>11,.2f}"
            )
        print("  " + "-" * 70)
        print(
            f"  {'TOTAL':<28}{len(zips):>8,}{zips.size_gb.sum():>12,.1f}"
            f"{zips.annual_cost.sum() / 12:>11,.2f}{zips.annual_cost.sum():>11,.2f}"
        )

        reclaim = zips[zips.status.str.startswith("ORPHAN")]
        print(
            f"\n  Reclaimable (ORPHAN_* only, age >= {args.min_age_days}d): "
            f"{len(reclaim):,} zips, {reclaim.size_gb.sum():,.1f} GB, "
            f"${reclaim.annual_cost.sum():,.2f}/year"
        )

        oldest = zips.nlargest(5, "age_days")[["key", "size_gb", "age_days", "status"]]
        print("\n  Oldest:")
        for _, r in oldest.iterrows():
            print(f"    {r.age_days:>5,}d  {r.size_gb:>7,.2f} GB  {r.status:<28} {r.key}")

        if args.write_manifest:
            path = OUTPUT_DIR / "orphaned_zips_DELETE_CANDIDATES.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(reclaim.key.tolist()) + "\n")
            print(f"\n  Wrote {len(reclaim):,} candidate keys -> {path.relative_to(REPO_ROOT)}")
            print("  Review before acting. This script deletes nothing.")

    if args.deep and prefixes:
        print(f"\n  Deep scan of {len(prefixes):,} batch-* prefixes (this lists every object)...")

        def scan(prefix):
            # botocore clients are thread-safe, so the paginator can be shared.
            paginator = s3.get_paginator("list_objects_v2")
            n = total = 0
            newest = None
            for page in paginator.paginate(Bucket=args.bucket, Prefix=prefix):
                for o in page.get("Contents", []):
                    n += 1
                    total += o["Size"]
                    if newest is None or o["LastModified"] > newest:
                        newest = o["LastModified"]
            bid = prefix.rstrip("/")
            b = (batches or {}).get(bid, {}) or {}
            return {
                "prefix": prefix,
                "objects": n,
                "size_gb": total / GB,
                "batch_exists": bool(b),
                "ingestion_complete": b.get("ingestionComplete"),
                "processing_end": b.get("processingEnd"),
                "cancelled": bool(b.get("stoppingInitiated")),
                "age_days": (now - newest).days if newest else None,
                "annual_cost": total / GB * S3_STANDARD_GB_MONTH * 12,
            }

        pre_rows = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(scan, p): p for p in prefixes}
            for i, fut in enumerate(as_completed(futures), 1):
                pre_rows.append(fut.result())
                if i % 10 == 0 or i == len(prefixes):
                    scanned = sum(r["objects"] for r in pre_rows)
                    print(f"    ...{i:,}/{len(prefixes):,} prefixes, {scanned:,} objects")

        pdf = pd.DataFrame(pre_rows).sort_values("size_gb", ascending=False)
        write_csv(pdf, "orphaned_batch_prefixes.csv")

        done = pdf[pdf.ingestion_complete.notna()]
        print(
            f"\n  Leftover extracted images: {pdf.objects.sum():,} objects, "
            f"{pdf.size_gb.sum():,.1f} GB, ${pdf.annual_cost.sum():,.2f}/year"
        )
        print(
            f"  Of those, {len(done):,} prefixes belong to COMPLETED batches "
            f"({done.size_gb.sum():,.1f} GB) and should already have been cleaned up."
        )

        print(f"\n  {'PREFIX':<44}{'OBJECTS':>10}{'GB':>10}{'AGE':>8}  STATE")
        print("  " + "-" * 84)
        for _, r in pdf.head(15).iterrows():
            state = (
                "cancelled" if r.cancelled
                else "ingestion complete" if pd.notna(r.ingestion_complete)
                else "no batch record" if not r.batch_exists
                else "incomplete"
            )
            age = f"{r.age_days:,}d" if pd.notna(r.age_days) else "?"
            print(f"  {r.prefix:<44}{r.objects:>10,}{r.size_gb:>10,.1f}{age:>8}  {state}")


if __name__ == "__main__":
    main()
