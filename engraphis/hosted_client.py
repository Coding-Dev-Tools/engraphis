"""Metadata and URL safety helpers for the hosted Engraphis service.

This module is deliberately not an entitlement engine.  Pro and Team authorization,
trial state, billing, signing, seat management, and feature execution are owned by the
private cloud control plane.  The public client keeps only safe destination metadata.
"""
from __future__ import annotations

import http.client
import ipaddress
import os
import socket
import urllib.request
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
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_addresses(host: str) -> list[str]:
    """Resolve *host* once and return only connection-safe numeric addresses."""

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if literal.is_loopback:
            return [str(literal)]
        if not literal.is_global:
            raise ValueError("cloud service URL must not target private/reserved IP ranges")
        return [str(literal)]

    try:
        resolved = socket.getaddrinfo(
            host, None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
    except (socket.gaierror, OSError):
        raise ValueError("cloud service URL could not be resolved") from None

    addresses = []
    loopback_name = _is_loopback_host(host)
    for _, _, _, _, sockaddr in resolved:
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if address.is_loopback and loopback_name:
            addresses.append(str(address))
            continue
        if not address.is_global:
            raise ValueError("cloud service URL must not target private/reserved IP ranges")
        addresses.append(str(address))
    if not addresses:
        raise ValueError("cloud service URL could not be resolved")
    return list(dict.fromkeys(addresses))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a vetted address with original-host TLS checks."""

    def __init__(self, host, *args, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._tls_server_hostname = self.host

    def set_tunnel(self, host, port=None, headers=None):
        # Make a configured proxy CONNECT to the vetted numeric target. TLS still
        # authenticates the original hostname after the tunnel is established.
        self._tls_server_hostname = host
        pinned = _validated_addresses(host)[0]
        return super().set_tunnel(pinned, port=port, headers=headers)

    def connect(self):
        targets = [self.host] if self._tunnel_host is not None else _validated_addresses(self.host)
        last_error = None
        for target in targets:
            try:
                self.sock = self._create_connection(
                    (target, self.port), self.timeout, self.source_address
                )
                break
            except OSError as exc:
                last_error = exc
        else:
            assert last_error is not None
            raise last_error
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tls_server_hostname
        )


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler using pinned connections for every HTTPS request."""

    def https_open(self, req):
        return self.do_open(
            PinnedHTTPSConnection,
            req,
            context=self._context,
            check_hostname=self._check_hostname,
        )


def build_pinned_https_opener(*handlers):
    """Build an opener that prevents DNS rebinding on credential-bearing HTTPS."""

    return urllib.request.build_opener(*handlers, PinnedHTTPSHandler())


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
        _validated_addresses(hostname)
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
