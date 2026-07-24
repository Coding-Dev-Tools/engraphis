"""Metadata and URL safety helpers for the hosted Engraphis service.

This module is deliberately not an entitlement engine.  Pro and Team authorization,
trial state, billing, signing, seat management, and feature execution are owned by the
private cloud control plane.  The public client keeps only safe destination metadata.
"""
from __future__ import annotations

import ipaddress
import os
import socket
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


TRIAL_DAYS = 3
TRIAL_SECONDS = 3 * 24 * 60 * 60
MAX_LOCAL_WRITE_GRACE_SECONDS = 24 * 60 * 60

# The hosted dashboard and the commercial account portal are separate surfaces.
# Upgrade/connect actions must land on the authenticated control-plane portal; the
# dashboard host does not serve its own ``/account`` route.
DEFAULT_CLOUD_URL = "https://api.engraphis.com/account"

_REQUIRED_PLAN = {
    "analytics": "pro",
    "automation": "pro",
    "consolidation": "pro",
    "dreaming": "pro",
    "export": "pro",
    "sync": "pro",
    "team": "team",
}


class HostedFeatureError(RuntimeError):
    """A hosted feature is unavailable to this local client.

    The exception contains presentation metadata only.  It never decides entitlement.
    The cloud service remains authoritative for every Pro and Team operation.
    """

    def __init__(self, message: str, *, feature: Optional[str] = None):
        super().__init__(message)
        self.feature = feature


def required_plan(feature: str) -> str:
    """Return the advertised minimum hosted plan for a feature."""

    return _REQUIRED_PLAN.get(str(feature or "").strip().lower(), "pro")


def upgrade_url(plan: Optional[str] = None) -> str:
    """Return the hosted account URL used by local upgrade/connect affordances."""

    name = str(plan or "pro").strip().lower()
    if name == "team":
        value = (
            os.environ.get("ENGRAPHIS_TEAM_UPGRADE_URL", "").strip()
            or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip()
        )
    else:
        value = (
            os.environ.get("ENGRAPHIS_PRO_UPGRADE_URL", "").strip()
            or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip()
        )
    return value or DEFAULT_CLOUD_URL


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_cloud_base_url(value: str) -> str:
    """Validate a cloud endpoint without reflecting its potentially sensitive value."""

    parts = urlsplit(str(value or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("cloud service URL must be an absolute http(s) URL")
    try:
        parts.port
    except ValueError:
        raise ValueError("cloud service URL has an invalid port") from None
    if parts.username is not None or parts.password is not None:
        raise ValueError("cloud service URL must not contain embedded credentials")
    if "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("cloud service URL contains an invalid host")
    if parts.query or parts.fragment:
        raise ValueError("cloud service URL must not contain a query string or fragment")
    hostname = parts.hostname.lower()
    if scheme != "https" and not _is_loopback_host(hostname):
        raise ValueError("cloud service URL must use HTTPS unless it targets loopback")
    if not _is_loopback_host(hostname):
        try:
            addresses = socket.getaddrinfo(
                hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM
            )
            for _, _, _, _, sockaddr in addresses:
                try:
                    address = ipaddress.ip_address(sockaddr[0])
                except ValueError:
                    continue
                if (
                    address.is_private
                    or address.is_reserved
                    or address.is_link_local
                    or address.is_multicast
                    or address.is_unspecified
                ):
                    raise ValueError(
                        "cloud service URL must not target private/reserved IP ranges"
                    )
        except (socket.gaierror, OSError):
            raise ValueError(
                "cloud service URL could not be resolved"
            ) from None
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
