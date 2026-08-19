########
#
# pricing_tiers.py
#
# Phase 5: turn the unit economics into candidate pricing tiers and check what
# they would actually recover.
#
# The cost of a project has two parts that behave differently: a one-time
# ingestion charge and a recurring retention charge that never stops. A flat
# annual price therefore loses money on any long-lived archive, while a pure
# per-image-ingested price under-charges the customers who keep data forever.
#
# Tier boundaries are evaluated against the real project distribution rather
# than round numbers, because that distribution is extremely concentrated: the
# largest project alone is ~21% of all stored images.
#
# Usage:
#
#   python3 -m utils.cost_analysis.pricing_tiers            # uses cached data
#   python3 -m utils.cost_analysis.pricing_tiers --refresh  # re-query MongoDB
#
########

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import OUTPUT_DIR, REPO_ROOT, write_csv

# From cost_model.py, clean-system rates.
COST_PER_IMAGE_INGESTED = 1.007 / 1000
COST_PER_IMAGE_MONTH = 0.0233 / 1000
PLATFORM_FIXED_MONTHLY = 293.0 + 46.85  # Atlas cluster + Route53/ECR/VPC

INGESTION_FILE = "mongo_ingestion_by_project.csv"


def fetch_ingestion_by_project():
    from dotenv import load_dotenv
    from pymongo import MongoClient, ReadPreference

    load_dotenv(REPO_ROOT / ".env")
    db = (
        MongoClient(os.environ["MONGODB_URL"], serverSelectionTimeoutMS=15000)
        .get_database()
        .with_options(read_preference=ReadPreference.SECONDARY_PREFERRED)
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=365)).replace(tzinfo=None)
    rows = db["images"].aggregate(
        [
            {"$match": {"dateAdded": {"$gte": cutoff}}},
            {"$group": {"_id": "$projectId", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}},
        ],
        allowDiskUse=True,
    )
    df = pd.DataFrame(rows).rename(columns={"_id": "project_id", "n": "ingested_12mo"})
    write_csv(df, INGESTION_FILE)
    return df


def load_projects(refresh):
    stock = pd.read_csv(OUTPUT_DIR / "mongo_images_by_project.csv")
    path = OUTPUT_DIR / INGESTION_FILE
    if refresh or not path.exists():
        ingestion = fetch_ingestion_by_project()
    else:
        ingestion = pd.read_csv(path)

    df = stock.merge(ingestion, on="project_id", how="left").fillna({"ingested_12mo": 0})
    df["annual_ingest_cost"] = df["ingested_12mo"] * COST_PER_IMAGE_INGESTED
    # Retention is charged on the average stock held through the year.
    df["avg_stock"] = df["images"] - df["ingested_12mo"] / 2
    df["annual_storage_cost"] = df["avg_stock"] * COST_PER_IMAGE_MONTH * 12
    df["annual_cost"] = df["annual_ingest_cost"] + df["annual_storage_cost"]
    return df.sort_values("annual_cost", ascending=False).reset_index(drop=True)


def charge(row, tier):
    """Base price plus any metered overage above the tier's included allowance.

    Overage is what stops an unbounded top tier being a blank cheque: without it,
    a fixed price has to be set for the worst customer imaginable rather than the
    typical one.
    """
    total = tier["price"]
    over_ingest = tier.get("overage_ingest_per_1k", 0)
    over_stored = tier.get("overage_stored_per_1k_mo", 0)
    if over_ingest:
        excess = max(0, row["ingested_12mo"] - tier.get("included_images", tier["images"]))
        total += excess / 1000 * over_ingest
    if over_stored:
        excess = max(0, row["avg_stock"] - tier.get("included_stored", tier["stored"]))
        total += excess / 1000 * over_stored * 12
    return total


def ceiling_cost(tier):
    """Annual cost of a customer sitting exactly at a tier's limits.

    This is the number that decides whether a tier can lose money, since within a
    tier a customer may use anything from zero up to the ceiling for one price.
    Margin measured on today's average customer flatters the result, because most
    customers sit well below their ceiling.
    """
    if tier["images"] > 10**11:
        return float("nan")  # unbounded tier
    ingest = tier["images"] * COST_PER_IMAGE_INGESTED
    storage = tier["stored"] * COST_PER_IMAGE_MONTH * 12
    return ingest + storage


def evaluate(df, tiers, label):
    """Assign each project to the cheapest tier whose limits it fits, then report."""
    def assign(row):
        for tier in tiers:
            if row["ingested_12mo"] <= tier["images"] and row["images"] <= tier["stored"]:
                return tier["name"]
        return tiers[-1]["name"]

    df = df.copy()
    df["tier"] = df.apply(assign, axis=1)
    by_name = {t["name"]: t for t in tiers}
    df["revenue"] = df.apply(lambda r: charge(r, by_name[r["tier"]]), axis=1)

    print(f"\n{'=' * 88}\n{label}\n{'=' * 88}")
    print(f"{'TIER':<12}{'PRICE/YR':>9}{'PROJ':>6}{'COST NOW':>10}{'REVENUE':>10}"
          f"{'MARGIN':>8}{'COST @ CEIL':>13}{'HEADROOM':>10}")
    print("-" * 88)
    for tier in tiers:
        sub = df[df.tier == tier["name"]]
        ceil = ceiling_cost(tier)
        ceil_s = f"{ceil:,.0f}" if ceil == ceil else "unbounded"
        if ceil != ceil or tier["price"] == 0:
            head = "-"
        else:
            head = f"{tier['price'] / ceil:.2f}x"
            if tier["price"] < ceil:
                head += " LOSS"
        if sub.empty:
            print(f"{tier['name']:<12}{tier['price']:>9,.0f}{0:>6}{'-':>10}{'-':>10}"
                  f"{'-':>8}{ceil_s:>13}{head:>10}")
            continue
        cost, revenue = sub["annual_cost"].sum(), sub["revenue"].sum()
        margin = f"{100 * (revenue - cost) / revenue:.0f}%" if revenue else "-"
        print(f"{tier['name']:<12}{tier['price']:>9,.0f}{len(sub):>6}{cost:>10,.0f}"
              f"{revenue:>10,.0f}{margin:>8}{ceil_s:>13}{head:>10}")

    total_cost = df["annual_cost"].sum() + PLATFORM_FIXED_MONTHLY * 12
    total_rev = df["revenue"].sum()
    print("-" * 88)
    print(f"{'TOTAL':<12}{'':>9}{len(df):>6}{total_cost:>10,.0f}{total_rev:>10,.0f}"
          f"{100 * (total_rev - total_cost) / total_rev if total_rev else 0:>7.0f}%")
    print(f"  COST NOW is what today's customers actually cost; COST @ CEIL is what a customer")
    print(f"  maxing out the tier would cost. HEADROOM below 1.0x means the tier can lose money.")

    losers = df[df["annual_cost"] > df["revenue"]]
    paying_losers = losers[losers["revenue"] > 0]
    if not paying_losers.empty:
        print(f"\n  {len(paying_losers)} PAYING projects already cost more than they pay:")
        for _, r in paying_losers.head(5).iterrows():
            print(f"    {str(r.project_id)[:34]:<34} {r.tier:<10} cost ${r.annual_cost:>8,.0f}"
                  f"  pays ${r.revenue:>7,.0f}")
    free_cost = losers[losers["revenue"] == 0]["annual_cost"].sum()
    print(f"  free tier costs ${free_cost:,.0f}/yr across {(df.revenue == 0).sum()} projects")
    return df


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--refresh", action="store_true", help="re-query MongoDB")
    args = p.parse_args()

    df = load_projects(args.refresh)
    write_csv(df, "project_cost_model.csv")

    total_cost = df["annual_cost"].sum() + PLATFORM_FIXED_MONTHLY * 12
    print(f"\nMODELLED ANNUAL COST ACROSS {len(df)} PROJECTS: ${total_cost:,.0f}")
    print(f"  ingestion ${df['annual_ingest_cost'].sum():,.0f}"
          f" | retention ${df['annual_storage_cost'].sum():,.0f}"
          f" | platform fixed ${PLATFORM_FIXED_MONTHLY * 12:,.0f}")

    print(f"\n{'PROJECT':<40}{'STORED':>11}{'INGESTED/YR':>13}{'COST/YR':>10}")
    print("-" * 74)
    for _, r in df.head(12).iterrows():
        print(f"{str(r.project_id)[:38]:<40}{r['images']:>11,.0f}"
              f"{r['ingested_12mo']:>13,.0f}{r['annual_cost']:>10,.2f}")

    active = (df["ingested_12mo"] > 0).sum()
    print(f"\n  {active} of {len(df)} projects ingested anything in the last 12 months")
    print(f"  {(df['annual_cost'] < 10).sum()} projects cost under $10/yr to run")
    print(f"  the largest project costs ${df['annual_cost'].max():,.0f}/yr "
          f"({100 * df['annual_cost'].max() / total_cost:.0f}% of total)")

    # Tiers are set where the project distribution actually has gaps.
    usage_tiers = [
        {"name": "Free", "images": 10_000, "stored": 50_000, "price": 0},
        {"name": "Starter", "images": 100_000, "stored": 500_000, "price": 600},
        {"name": "Standard", "images": 500_000, "stored": 2_000_000, "price": 2_400},
        {"name": "Pro", "images": 2_000_000, "stored": 8_000_000, "price": 7_200},
        {"name": "Enterprise", "images": 10**12, "stored": 10**12, "price": 18_000},
    ]
    evaluate(df, usage_tiers, "OPTION A — usage tiers on ingestion + storage")

    lean_tiers = [
        {"name": "Free", "images": 25_000, "stored": 100_000, "price": 0},
        {"name": "Starter", "images": 250_000, "stored": 1_000_000, "price": 300},
        {"name": "Standard", "images": 1_000_000, "stored": 4_000_000, "price": 1_200},
        {"name": "Pro", "images": 5_000_000, "stored": 20_000_000, "price": 3_600},
        {"name": "Enterprise", "images": 10**12, "stored": 10**12, "price": 9_000},
    ]
    evaluate(df, lean_tiers, "OPTION B — cost-recovery pricing (roughly 2x cost)")

    print(f"\n{'=' * 74}\nOPTION C — pure usage-based\n{'=' * 74}")
    for markup in (2, 3, 4):
        ing = COST_PER_IMAGE_INGESTED * 1000 * markup
        ret = COST_PER_IMAGE_MONTH * 1000 * markup
        revenue = (
            df["ingested_12mo"].sum() * COST_PER_IMAGE_INGESTED * markup
            + df["avg_stock"].sum() * COST_PER_IMAGE_MONTH * 12 * markup
        )
        margin = 100 * (revenue - total_cost) / revenue if revenue else 0
        print(f"  {markup}x markup: ${ing:.2f} per 1k ingested + ${ret:.3f} per 1k stored/mo"
              f"  -> ${revenue:,.0f}/yr revenue, {margin:.0f}% margin")

    # Ceilings sized so every tier keeps ~1.5x headroom over its own worst case,
    # which is what Option B failed to do, plus overage so the top tier is bounded.
    recovery_tiers = [
        {"name": "Free", "images": 25_000, "stored": 100_000, "price": 0},
        {"name": "Starter", "images": 100_000, "stored": 350_000, "price": 300},
        {"name": "Standard", "images": 400_000, "stored": 1_400_000, "price": 1_200},
        {"name": "Pro", "images": 1_200_000, "stored": 4_200_000, "price": 3_600},
        {
            "name": "Enterprise",
            "images": 10**12,
            "stored": 10**12,
            "price": 3_600,
            "included_images": 1_200_000,
            "included_stored": 4_200_000,
            "overage_ingest_per_1k": 2.00,
            "overage_stored_per_1k_mo": 0.047,
        },
    ]
    evaluate(df, recovery_tiers, "OPTION D — corrected cost recovery (ceilings sized for headroom, top tier metered)")


if __name__ == "__main__":
    main()
