########
#
# driver_model.py
#
# Phase 3: attribute each AWS usage type to a cost driver and estimate how much
# it moves when that driver moves.
#
# Every usage type is regressed only on the drivers that can plausibly cause it
# (storage on the stock of stored images, request charges on daily flow, and so
# on). Throwing all drivers at every usage type would produce better-looking R^2
# and meaningless coefficients, because cumulative stock is the integral of
# daily flow and the two are nearly collinear.
#
# Daily cost data is strongly autocorrelated, so standard errors are Newey-West
# (HAC). The Dec 2025 - Jan 2026 duplicate-upload incident is carried as a dummy
# rather than dropped, so it cannot masquerade as a usage effect.
#
# Reads only CSVs produced by the other scripts. No AWS or MongoDB calls.
#
# Usage:
#
#   python3 -m utils.cost_analysis.driver_model
#
########

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.cost_analysis.common import OUTPUT_DIR, write_csv

MATERIALITY = 50.0  # ignore usage types below this over the whole window
ANOMALY = ("2025-12-01", "2026-01-31")

# driver key -> which usage types it is allowed to explain
DRIVERS = {
    "stock": "B_retention",
    "images": "A_ingestion",
    "inferences": "A_ingestion",
    "user_activity": "D_user_activity",
}


def classify(service, usage_type):
    """Assign a usage type to a cost driver on mechanism, not correlation."""
    u, s = str(usage_type), str(service)

    if "Registrar" in s or "Route 53" in s or "Certificate" in s:
        return "E_fixed", None
    if "CloudFront" in s:
        return "D_user_activity", "user_activity"
    if "ECR" in s or "Container Registry" in s:
        return "C_model_catalog", None
    if "TimedStorage" in u:
        # Storage accrues on the stock of data, never on the daily flow.
        return "B_retention", "stock"
    if "SecretsPerMonth" in u or "Secret" in u and "APIRequest" not in u:
        return "E_fixed", None
    if s == "Amazon SageMaker":
        return "A_ingestion", "inferences"
    if "Requests-Tier" in u or "EarlyDelete" in u:
        return "A_ingestion", "images"
    if "DataTransfer-Out" in u and s == "Amazon Simple Storage Service":
        return "D_user_activity", "user_activity"
    if s == "Amazon API Gateway":
        return "D_user_activity", "user_activity"
    if s == "AWS Lambda":
        return "A_ingestion", "images"
    if s == "AmazonCloudWatch":
        return "A_ingestion", "images"
    if "Secrets Manager" in s or "Systems Manager" in s:
        return "A_ingestion", "images"
    if "MongoDB" in s:
        return "B_retention", "stock"
    if "VPC" in s or "Elastic Container Service" in s or "Queue" in s or "SQS" in s:
        return "A_ingestion", "images"
    return "E_fixed", None


def build_drivers():
    """Daily driver series: ingestion flow, stored stock, inference count, user activity."""
    images = pd.read_csv(OUTPUT_DIR / "mongo_daily_images.csv", parse_dates=["date"])
    stock = pd.read_csv(OUTPUT_DIR / "mongo_daily_image_stock.csv", parse_dates=["date"])

    sm_metrics = pd.read_csv(OUTPUT_DIR / "sagemaker_daily_metrics.csv", parse_dates=["date"])
    inferences = (
        sm_metrics[sm_metrics.metric == "Invocations_Sum"]
        .groupby("date")["value"]
        .sum()
        .rename("inferences")
    )

    lam = pd.read_csv(OUTPUT_DIR / "lambda_daily_metrics.csv", parse_dates=["date"])
    graphql = (
        lam[(lam.function_name == "animl-api-prod-graphql") & (lam.metric == "Invocations")]
        .groupby("date")["value"]
        .sum()
        .rename("graphql")
    )

    df = (
        images.rename(columns={"count": "images"})
        .set_index("date")[["images"]]
        .join(stock.set_index("date")[["cumulative"]].rename(columns={"cumulative": "stock"}))
        .join(inferences)
        .join(graphql)
        .fillna(0.0)
    )

    # The frontend leaves no direct trace, so isolate the share of graphql traffic
    # that ingestion cannot account for and use that as the user-activity proxy.
    X = sm.add_constant(df[["images", "inferences"]])
    fit = sm.OLS(df["graphql"], X).fit()
    df["user_activity"] = (fit.resid + fit.params["const"]).clip(lower=0)
    df.attrs["graphql_fit"] = fit
    return df


def fit_one(y, drivers, driver_key, anomaly):
    """Regress one usage type on its assigned driver plus an anomaly dummy."""
    cols = ["anomaly"] if driver_key is None else [driver_key, "anomaly"]
    X = pd.concat([drivers[[c for c in cols if c != "anomaly"]], anomaly], axis=1)
    X = sm.add_constant(X, has_constant="add")
    model = sm.OLS(y, X, missing="drop")
    # Daily spend is autocorrelated; plain OLS errors would be far too optimistic.
    return model.fit(cov_type="HAC", cov_kwds={"maxlags": 14})


def main():
    ce = pd.read_csv(OUTPUT_DIR / "ce_daily_service_usagetype.csv", parse_dates=["date"])
    drivers = build_drivers()

    fit = drivers.attrs["graphql_fit"]
    print("graphql invocations ~ images + inferences")
    print(f"  R^2 {fit.rsquared:.3f} | per image {fit.params['images']:.2f}"
          f" | per inference {fit.params['inferences']:.2f}"
          f" | baseline {fit.params['const']:,.0f}/day")
    share = (drivers['user_activity'].sum() / drivers['graphql'].sum()) * 100
    print(f"  => ~{share:.0f}% of graphql traffic is not explained by ingestion\n")

    ce["bucket"], ce["driver"] = zip(*ce.apply(lambda r: classify(r.service, r.usage_type), axis=1))
    totals = ce.groupby(["service", "usage_type", "bucket", "driver"], dropna=False)[
        "unblended_cost"
    ].sum()
    material = totals[totals > MATERIALITY].sort_values(ascending=False)

    anomaly = pd.Series(
        ((drivers.index >= ANOMALY[0]) & (drivers.index <= ANOMALY[1])).astype(float),
        index=drivers.index,
        name="anomaly",
    )

    rows = []
    print(f"{'USAGE TYPE':<42}{'DRIVER':<14}{'TOTAL $':>9}{'$/1k UNITS':>12}{'R2':>6}{'p':>7}  FIT")
    print("-" * 100)
    for (service, usage_type, bucket, driver_key), total in material.items():
        driver_key = None if (driver_key is None or pd.isna(driver_key)) else driver_key
        series = (
            ce[(ce.service == service) & (ce.usage_type == usage_type)]
            .groupby("date")["unblended_cost"]
            .sum()
            .reindex(drivers.index, fill_value=0.0)
        )
        if series.std() == 0:
            continue

        usable = driver_key if driver_key in drivers else None
        res = fit_one(series, drivers, usable, anomaly)
        key = usable or "const"
        coef = res.params.get(key, res.params["const"])
        pval = res.pvalues.get(key, res.pvalues["const"])

        # Say plainly where the linear specification does not hold.
        if usable is None:
            verdict = "fixed"
        elif pval > 0.05:
            verdict = "NOT SIGNIFICANT"
        elif res.rsquared < 0.30:
            verdict = "weak"
        elif res.rsquared < 0.60:
            verdict = "moderate"
        else:
            verdict = "good"

        rows.append(
            {
                "service": service,
                "usage_type": usage_type,
                "bucket": bucket,
                "driver": usable or "fixed",
                "total_cost": total,
                "coef": coef,
                "cost_per_1k_units": coef * 1000,
                "p_value": pval,
                "r2": res.rsquared,
                "fit": verdict,
                "anomaly_effect": res.params.get("anomaly", np.nan),
                "daily_fixed": res.params["const"],
            }
        )
        label = usage_type if len(usage_type) < 40 else usage_type[:37] + "..."
        per_1k = f"{coef * 1000:>12,.4f}" if usable else f"{'-':>12}"
        print(f"{label:<42}{(usable or 'fixed'):<14}{total:>9,.0f}{per_1k}"
              f"{res.rsquared:>6.2f}{pval:>7.3f}  {verdict}")

    out = pd.DataFrame(rows)
    write_csv(out, "driver_model_coefficients.csv")

    print("\n\nDRIVER SHARE OF SPEND")
    print("-" * 62)
    by_bucket = out.groupby("bucket")["total_cost"].sum().sort_values(ascending=False)
    names = {
        "A_ingestion": "A. Ingestion volume",
        "B_retention": "B. Data retention",
        "C_model_catalog": "C. Model catalog",
        "D_user_activity": "D. User / frontend activity",
        "E_fixed": "E. Fixed",
    }
    for bucket, value in by_bucket.items():
        print(f"  {names.get(bucket, bucket):<34}${value:>10,.2f}{100 * value / by_bucket.sum():>7.1f}%")

    print("\n\nELASTICITY — extra $/MONTH if a driver doubles from today's level")
    print("-" * 82)
    # Flow drivers are per-day rates; stock is a level. Convert both to a monthly
    # delta so the column is comparable.
    levels = {
        "images": drivers["images"].mean() * 30.44,
        "inferences": drivers["inferences"].mean() * 30.44,
        "stock": drivers["stock"].iloc[-1],
        "user_activity": drivers["user_activity"].mean() * 30.44,
    }
    print(f"  {'DRIVER':<18}{'CURRENT LEVEL':>18}{'EXTRA $/MONTH':>16}   BASED ON")
    for driver_key, level in levels.items():
        affected = out[(out.driver == driver_key) & (out.fit != "NOT SIGNIFICANT")]
        if affected.empty:
            print(f"  {driver_key:<18}{level:>18,.0f}{'n/a':>16}   no significant relationship")
            continue
        if driver_key == "stock":
            # coef is $/day per stored image, so scale to a month.
            delta = (affected["coef"] * level).sum() * 30.44
        else:
            delta = (affected["coef"] * level).sum()
        types = ", ".join(sorted(set(affected.usage_type.str.replace("USW2-", "", regex=False))))
        print(f"  {driver_key:<18}{level:>18,.0f}{delta:>16,.2f}   {types[:44]}")

    weak = out[out.fit.isin(["NOT SIGNIFICANT", "weak"])]
    if not weak.empty:
        print("\n  Usage types the linear model does NOT explain well:")
        for _, r in weak.iterrows():
            print(f"    {r.usage_type:<42} ${r.total_cost:>8,.0f}  R2={r.r2:.2f}  {r.fit}")

    print(f"\n  Dec-Jan anomaly effect: ${out['anomaly_effect'].sum():,.2f}/day while active")


if __name__ == "__main__":
    main()
