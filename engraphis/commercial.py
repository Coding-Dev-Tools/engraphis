"""Public commercial manifest helpers.

The public package contains customer-side entitlement and managed-service clients only.
Billing, fulfillment, license issuance, hosted relay authority, and operational readiness
live in the private service repository and are intentionally not importable here.
"""
from __future__ import annotations

import json
from pathlib import Path


PRODUCT_ENV = {
    "POLAR_PRO_MONTHLY_PRODUCT_ID": ("pro", "monthly"),
    "POLAR_PRO_ANNUAL_PRODUCT_ID": ("pro", "annual"),
    "POLAR_TEAM_MONTHLY_PRODUCT_ID": ("team", "monthly"),
    "POLAR_TEAM_ANNUAL_PRODUCT_ID": ("team", "annual"),
}


def manifest() -> dict:
    """Load the public plan and product manifest used by release checks."""
    path = Path(__file__).with_name("commercial_manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


def expected_product_ids() -> dict:
    """Return the manifest's public product identifiers keyed by environment name."""
    expected = {}
    for plan_name in ("pro", "team"):
        for interval, product in manifest()["plans"][plan_name]["products"].items():
            expected[product["env"]] = {
                "id": product["id"],
                "plan": plan_name,
                "interval": interval,
            }
    return expected
