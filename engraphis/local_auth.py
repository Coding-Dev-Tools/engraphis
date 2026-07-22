"""Minimal authentication for the single-user local runtime.

Hosted identities, organizations, roles, invitations, seats, sessions, and recovery
belong to Engraphis Cloud.  The open package supports only one optional deployment
secret for machine-to-machine access: ``ENGRAPHIS_API_TOKEN``.
"""
from __future__ import annotations

import hmac
from typing import Optional


def bearer_token(authorization: Optional[str]) -> str:
    """Return a stripped bearer credential, or an empty string for another scheme."""
    value = str(authorization or "")
    if value[:7].lower() != "bearer ":
        return ""
    return value[7:].strip()


def bearer_ok(authorization: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time validation for the local runtime's optional API token."""
    configured = str(expected or "")
    supplied = bearer_token(authorization)
    return bool(configured and supplied) and hmac.compare_digest(supplied, configured)
