"""Client-side interpretation of Engraphis Cloud authorization denials.

The control plane returns structured JSON bodies on 401/402/403 with a
``reason`` field. This module maps those reasons to user-facing messages
and UI actions (e.g. showing an upgrade prompt). All actual authorization
decisions are made server-side; this module only translates the verdict for
the local dashboard's presentation layer.

Never trust these values for access control. They are UX hints derived from
an authoritative server response that has already been validated by
``cloud_session`` and ``cloud_features``.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger("engraphis.cloud_authz")

# Machine-readable reason codes the control plane may return.
# Keep in sync with engraphis_cloud/services/team_authorization.py and
# engraphis_cloud/api/control/_helpers.py.
REASON_ORG_SUSPENDED = "organization_suspended"
REASON_ENTITLEMENT_EXPIRED = "entitlement_expired"
REASON_SEAT_LIMIT_EXCEEDED = "seat_limit_exceeded"
REASON_CROSS_ORG_DENIED = "cross_org_access_denied"
REASON_DEPLOYMENT_TOKEN_ESCALATION = "deployment_token_cannot_escalate"
REASON_INSUFFICIENT_ROLE = "insufficient_role"
REASON_MEMBER_DISABLED = "member_disabled"
REASON_TOKEN_STALE = "token_stale"

_DENIAL_MESSAGES: dict[str, str] = {
    REASON_ORG_SUSPENDED: (
        "This organization has been suspended. Contact support to restore access."
    ),
    REASON_ENTITLEMENT_EXPIRED: (
        "Your subscription has expired. Renew your plan to continue using Team features."
    ),
    REASON_SEAT_LIMIT_EXCEEDED: (
        "All named seats are in use. Upgrade your plan to add more team members."
    ),
    REASON_CROSS_ORG_DENIED: (
        "Access denied: this credential belongs to a different organization."
    ),
    REASON_DEPLOYMENT_TOKEN_ESCALATION: (
        "Deployment tokens cannot perform administrative actions. "
        "Sign in with a member account instead."
    ),
    REASON_INSUFFICIENT_ROLE: (
        "You do not have permission to perform this action. "
        "Ask an organization owner or administrator."
    ),
    REASON_MEMBER_DISABLED: (
        "Your account has been disabled. Contact your organization administrator."
    ),
    REASON_TOKEN_STALE: (
        "Your session is out of date. Please sign in again."
    ),
}

_UPGRADE_REASONS = frozenset({
    REASON_ENTITLEMENT_EXPIRED,
    REASON_SEAT_LIMIT_EXCEEDED,
})


def interpret_denial(
    status_code: int,
    body: Optional[dict[str, Any]],
) -> dict[str, Any]:
    """Translate a cloud denial into a dashboard-friendly structure.

    Returns ``message``, ``reason``, ``upgrade_url``, and ``retryable`` fields.
    Never raises; malformed or unexpected inputs degrade to a generic denial.
    """
    if not isinstance(body, dict):
        return _generic_denial(status_code)

    reason = str(body.get("reason") or "").strip()
    if not reason:
        detail = body.get("detail") or body.get("error") or ""
        if isinstance(detail, dict):
            reason = str(detail.get("reason") or "")
        elif isinstance(detail, str):
            reason = detail

    message = _DENIAL_MESSAGES.get(reason)
    if message is None:
        logger.warning(
            "unrecognized cloud denial reason=%r status=%s",
            reason, status_code,
        )
        return _generic_denial(status_code)

    result: dict[str, Any] = {
        "message": message,
        "reason": reason,
        "upgrade_url": None,
        "retryable": reason == REASON_TOKEN_STALE,
    }
    upgrade_url = body.get("upgrade_url")
    if upgrade_url and isinstance(upgrade_url, str):
        result["upgrade_url"] = upgrade_url
    elif reason in _UPGRADE_REASONS:
        result["upgrade_url"] = "/billing/upgrade"
    return result


def _generic_denial(status_code: int) -> dict[str, Any]:
    """Return a safe fallback for an unrecognized or malformed denial."""
    if status_code == 402:
        message = "A paid subscription is required for this feature."
    elif status_code == 401:
        message = "Your session has expired. Please sign in again."
    else:
        message = "Access denied. Contact your organization administrator."
    return {
        "message": message,
        "reason": "unknown",
        "upgrade_url": None,
        "retryable": False,
    }


def is_authoritative_denial(status_code: int) -> bool:
    """Return whether *status_code* is a definitive cloud authorization verdict."""
    return status_code in {401, 402, 403}
