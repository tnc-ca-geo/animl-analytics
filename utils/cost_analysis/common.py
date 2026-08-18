########
#
# common.py
#
# Shared configuration and helpers for the Animl AWS cost analysis.
#
# All AWS calls made by this package are READ-ONLY.
#
# Authentication goes through aws-vault only. Credentials stay in the OS keychain
# and are never read from ~/.aws/credentials by this code.
#
########

import os
import sys
from pathlib import Path

import boto3

AWS_REGION = os.environ.get("ANIML_AWS_REGION", "us-west-2")

# Cost Explorer and the Price List API are only available in us-east-1.
BILLING_REGION = "us-east-1"

ACCOUNT_ID = "830244800171"

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = REPO_ROOT / "outputs" / "cost-analysis"
DATA_DIR = REPO_ROOT / "data" / "cost-analysis"

VAULT_PROFILE = os.environ.get("ANIML_AWS_PROFILE", "animl")

_NO_CREDS_HINT = f"""
No AWS credentials found in the environment.

This package authenticates via aws-vault so that credentials stay in the OS
keychain; it deliberately will not fall back to reading ~/.aws/credentials.

Re-run the command under aws-vault, e.g.:

    aws-vault exec {VAULT_PROFILE} -- python -m utils.cost_analysis.<script>

aws-vault caches a temporary STS session, so it only prompts for your keychain
password when that cache is cold. To force it to prompt every time:

    aws-vault exec --no-session {VAULT_PROFILE} -- python -m utils.cost_analysis.<script>
"""


class MissingVaultCredentials(RuntimeError):
    """Raised when the process was not launched under `aws-vault exec`."""


def get_session():
    """Build a boto3 Session from aws-vault-injected environment credentials.

    Never passes profile_name: that would read ~/.aws/credentials off disk and
    bypass the keychain prompt entirely.
    """
    has_creds = os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get(
        "AWS_SECRET_ACCESS_KEY"
    )
    if not has_creds:
        raise MissingVaultCredentials(_NO_CREDS_HINT)

    if not os.environ.get("AWS_VAULT"):
        print(
            "  warning: AWS credentials are set but AWS_VAULT is not; these do not\n"
            "           appear to have come from aws-vault.",
            file=sys.stderr,
        )

    return boto3.Session(region_name=AWS_REGION)


def get_client(service, session=None, region=AWS_REGION):
    session = session or get_session()
    return session.client(service, region_name=region)


def ensure_dirs():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def write_csv(df, filename, output_dir=None):
    """Write a dataframe to the cost-analysis output dir and report what happened."""
    output_dir = output_dir or OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / filename
    df.to_csv(path, index=False)
    print(f"  wrote {len(df):>7,} rows -> {path.relative_to(REPO_ROOT)}")
    return path
