"""Public commercial manifest helpers.

The public package contains customer-side entitlement and managed-service clients only.
Billing, fulfillment, license issuance, hosted relay authority, and operational readiness
live in the private service repository and are intentionally not importable here.
"""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


BILLING_AUTHORITY = "stripe"


def _manifest_path() -> Path:
    return Path(__file__).with_name("commercial_manifest.json")


@lru_cache(maxsize=4)
def _load_manifest(path_str: str, mtime_ns: int, size: int) -> dict[str, Any]:
    """Parse the manifest for one ``(path_str, mtime_ns, size)`` file identity.

    The cache key combines the normalized path with the file's modification time and byte
    size so distinct paths never collide and normal file updates or package upgrades are
    re-read automatically. An in-place rewrite that preserves both ``st_mtime_ns`` and
    ``st_size`` keeps the cached entry until ``reload()`` is called.
    """

    del mtime_ns, size
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def manifest(path: Optional[Path] = None) -> dict[str, Any]:
    """Load the public plan and product manifest used by release checks.

    Returns a deep copy of the cached table (trading ~0.1 ms of ``stat`` + ``deepcopy`` for
    isolation) so caller mutations cannot corrupt process-wide state.
    """

    target = Path(path) if path is not None else _manifest_path()
    stat = target.stat()
    return copy.deepcopy(
        _load_manifest(str(target.absolute()), stat.st_mtime_ns, stat.st_size)
    )


def reload() -> None:
    """Drop the cached manifest. Tests and release tooling only."""

    _load_manifest.cache_clear()


def trial_days_by_plan() -> dict[str, int]:
    """Return disclosed plan durations; missing or invalid durations stay unknown."""
    try:
        days = manifest().get("trial", {}).get("days_by_plan", {})
    except (OSError, ValueError, TypeError, AttributeError):
        return {}
    if not isinstance(days, dict):
        return {}
    return {plan: value for plan, value in days.items()
            if plan in {"pro", "team"} and isinstance(value, int)
            and not isinstance(value, bool) and value > 0}


def expected_checkout_targets() -> dict:
    """Return public onboarding targets without exposing provider-side price identifiers."""
    # Read defensively: the release check calls this *after* collecting its own structural
    # errors, so a missing plan/products block must not raise a KeyError here and abort
    # before those errors are printed. Missing entries simply do not appear.
    plans = manifest().get("plans", {})
    expected = {}
    for plan_name in ("pro", "team"):
        products = plans.get(plan_name, {}).get("products", {})
        for interval, product in products.items():
            expected[(plan_name, interval)] = {
                "provider": product.get("provider"),
                "checkout_url": product.get("checkout_url"),
                "plan": plan_name,
                "interval": interval,
            }
    return expected
