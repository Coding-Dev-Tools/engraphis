"""Public commercial manifest helpers.

The public package contains customer-side entitlement and managed-service clients only.
Billing, fulfillment, license issuance, hosted relay authority, and operational readiness
live in the private service repository and are intentionally not importable here.
"""
from __future__ import annotations

import json
from pathlib import Path


BILLING_AUTHORITY = "stripe"


def manifest() -> dict:
    """Load the public plan and product manifest used by release checks."""
    path = Path(__file__).with_name("commercial_manifest.json")
    return json.loads(path.read_text(encoding="utf-8"))


def expected_checkout_targets() -> dict:
    """Return public onboarding targets without exposing provider-side price identifiers."""
    expected = {}
    for plan_name in ("pro", "team"):
        for interval, product in manifest()["plans"][plan_name]["products"].items():
            expected[(plan_name, interval)] = {
                "provider": product["provider"],
                "checkout_url": product["checkout_url"],
                "plan": plan_name,
                "interval": interval,
            }
    return expected
