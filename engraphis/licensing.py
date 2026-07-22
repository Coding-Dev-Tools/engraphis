"""Compatibility facade for hosted-plan presentation metadata.

The public package no longer parses, stores, signs, verifies, activates, or gates paid
license keys.  Pro and Team authorization is performed by Engraphis Cloud and reaches
this client only as short-lived scoped bearer credentials.  New code should import
``engraphis.hosted_client`` directly; this facade keeps older HTTP presentation imports
working while callers migrate.
"""
from __future__ import annotations

from engraphis.hosted_client import (
    MAX_LOCAL_WRITE_GRACE_SECONDS,
    TRIAL_DAYS,
    TRIAL_SECONDS,
    HostedFeatureError,
    required_plan,
    upgrade_url,
)


LicenseError = HostedFeatureError


def production_warnings() -> list:
    """Return no local signer warnings because the public package has no signer."""

    return []


__all__ = [
    "LicenseError",
    "MAX_LOCAL_WRITE_GRACE_SECONDS",
    "TRIAL_DAYS",
    "TRIAL_SECONDS",
    "production_warnings",
    "required_plan",
    "upgrade_url",
]
