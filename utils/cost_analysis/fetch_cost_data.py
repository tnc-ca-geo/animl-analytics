########
#
# fetch_cost_data.py
#
# Phase 1 of the Animl AWS cost analysis: pull cost data from the Cost Explorer
# API at DAILY granularity grouped by USAGE_TYPE.
#
# The billing console's service-level export cannot separate storage (a function
# of accumulated data) from requests (a function of throughput) from data
# transfer (a function of user activity). Usage-type granularity can, and daily
# granularity turns 6 monthly observations into ~400 daily ones, which is what
# makes the regression in Phase 3 viable.
#
# READ-ONLY. Cost Explorer bills $0.01 per API request; a full run is a few
# dozen requests.
#
# Usage:
#
#   python3 utils/cost_analysis/fetch_cost_data.py --months 13
#
########

import argparse
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import BILLING_REGION, get_client, get_session, write_csv

# Cost Explorer retains daily granularity for 14 months.
MAX_DAILY_MONTHS = 13


def paginate_cost_and_usage(client, **kwargs):
    """Yield every ResultsByTime page, following NextPageToken."""
    token = None
    while True:
        if token:
            kwargs["NextPageToken"] = token
        resp = client.get_cost_and_usage(**kwargs)
        yield from resp["ResultsByTime"]
        token = resp.get("NextPageToken")
        if not token:
            return
        time.sleep(0.2)  # CE throttles aggressively on wide daily queries


def flatten(results, group_names):
    """Flatten CE ResultsByTime into tidy rows, one per date/group combination."""
    rows = []
    for period in results:
        start = period["TimePeriod"]["Start"]
        groups = period.get("Groups") or []

        if not groups:
            # No GroupBy: totals live on the period itself.
            total = period.get("Total") or {}
            if not total:
                continue
            rows.append(
                {
                    "date": start,
                    "unblended_cost": float(total["UnblendedCost"]["Amount"]),
                    "usage_quantity": float(total.get("UsageQuantity", {}).get("Amount", 0)),
                    "usage_unit": total.get("UsageQuantity", {}).get("Unit", ""),
                }
            )
            continue

        for group in groups:
            row = {"date": start}
            for name, key in zip(group_names, group["Keys"]):
                row[name] = key
            metrics = group["Metrics"]
            row["unblended_cost"] = float(metrics["UnblendedCost"]["Amount"])
            row["usage_quantity"] = float(metrics.get("UsageQuantity", {}).get("Amount", 0))
            row["usage_unit"] = metrics.get("UsageQuantity", {}).get("Unit", "")
            rows.append(row)

    df = pd.DataFrame(rows)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def fetch(client, start, end, granularity, group_by=None, metrics=None, filter_=None):
    kwargs = {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": granularity,
        "Metrics": metrics or ["UnblendedCost", "UsageQuantity"],
    }
    if group_by:
        kwargs["GroupBy"] = [{"Type": "DIMENSION", "Key": k} for k in group_by]
    if filter_:
        kwargs["Filter"] = filter_

    results = list(paginate_cost_and_usage(client, **kwargs))
    return flatten(results, [k.lower() for k in (group_by or [])])


def month_floor(d):
    return d.replace(day=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--months",
        type=int,
        default=MAX_DAILY_MONTHS,
        help=f"months of history to pull (max {MAX_DAILY_MONTHS} for daily granularity)",
    )
    args = parser.parse_args()

    months = min(args.months, MAX_DAILY_MONTHS)

    # CE end date is exclusive; use tomorrow so today's partial data is included.
    end = date.today() + timedelta(days=1)
    start = month_floor(end - timedelta(days=months * 31))

    session = get_session()
    client = get_client("ce", session=session, region=BILLING_REGION)

    print(f"Cost Explorer pull: {start} -> {end} ({months} months)\n")

    try:
        # 1. Monthly by service. Reconciles directly against the billing console
        #    CSV export and is the top-level sanity check for everything else.
        print("[1/5] monthly by SERVICE")
        monthly_service = fetch(client, start, end, "MONTHLY", ["SERVICE"])
        write_csv(monthly_service, "ce_monthly_service.csv")

        # 2. Monthly by record type. The console export typically excludes
        #    credits/refunds/tax, so this explains any reconciliation gap.
        print("[2/5] monthly by RECORD_TYPE")
        monthly_record = fetch(client, start, end, "MONTHLY", ["RECORD_TYPE"])
        write_csv(monthly_record, "ce_monthly_recordtype.csv")

        # 3. Daily by service. The regression's dependent variable at service level.
        print("[3/5] daily by SERVICE")
        daily_service = fetch(client, start, end, "DAILY", ["SERVICE"])
        write_csv(daily_service, "ce_daily_service.csv")

        # 4. Daily by service + usage type. This is the core dataset: it splits
        #    S3 into storage vs requests vs transfer, CloudWatch into log
        #    ingestion vs log storage vs alarms, and SageMaker into serverless
        #    GB-seconds vs any always-on instance hours.
        print("[4/5] daily by SERVICE + USAGE_TYPE  (core dataset)")
        daily_usage = fetch(client, start, end, "DAILY", ["SERVICE", "USAGE_TYPE"])
        write_csv(daily_usage, "ce_daily_service_usagetype.csv")

        # 5. Daily by operation for the two services where operation-level detail
        #    changes the interpretation (S3 GET vs PUT, Lambda invoke paths).
        print("[5/5] daily by SERVICE + OPERATION")
        daily_operation = fetch(client, start, end, "DAILY", ["SERVICE", "OPERATION"])
        write_csv(daily_operation, "ce_daily_service_operation.csv")

    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDeniedException", "UnauthorizedException"):
            print(
                f"\nAccess denied ({code}).\n"
                "Cost Explorer needs BOTH:\n"
                "  1. ce:GetCostAndUsage on the IAM principal, and\n"
                "  2. IAM access to Billing enabled in the root account:\n"
                "     Billing Console > Account Settings > IAM User and Role Access to Billing\n",
                file=sys.stderr,
            )
            sys.exit(1)
        raise

    print("\n--- monthly totals (reconcile against costs CSV) ---")
    totals = monthly_service.groupby(monthly_service["date"].dt.strftime("%Y-%m"))[
        "unblended_cost"
    ].sum()
    for month, amount in totals.items():
        print(f"  {month}  ${amount:>12,.2f}")

    print("\n--- top 15 usage types by cost over the full window ---")
    top = (
        daily_usage.groupby(["service", "usage_type"])["unblended_cost"]
        .sum()
        .sort_values(ascending=False)
        .head(15)
    )
    for (service, usage_type), amount in top.items():
        print(f"  ${amount:>10,.2f}  {service:<32} {usage_type}")


if __name__ == "__main__":
    main()
