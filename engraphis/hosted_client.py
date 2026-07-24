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
        self._tunnel_targets = []

    def set_tunnel(self, host, port=None, headers=None):
        # Make a configured proxy CONNECT to the vetted numeric target. TLS still
        # authenticates the original hostname after the tunnel is established.
        # urllib passes ``Request.host`` straight through, and that netloc may carry an
        # explicit port, so split it the way http.client does before resolving or pinning
        # the SNI name -- otherwise ``cloud.example:8443`` is looked up verbatim and fails.
        hostname, tunnel_port = self._get_hostport(host, port)
        self._tls_server_hostname = hostname
        self._tunnel_targets = _validated_addresses(hostname)
        return super().set_tunnel(self._tunnel_targets[0], port=tunnel_port, headers=headers)

    def connect(self):
        if self._tunnel_host is not None:
            self._connect_through_proxy()
        else:
            self.sock = self._connect_directly()
        self.sock = self._context.wrap_socket(
            self.sock, server_hostname=self._tls_server_hostname
        )

    def _connect_directly(self):
        last_error = None
        for target in _validated_addresses(self.host):
            try:
                return self._create_connection(
                    (target, self.port), self.timeout, self.source_address
                )
            except OSError as exc:
                last_error = exc
        if last_error is None:
            raise OSError("cloud service URL has no connectable address")
        raise last_error

    @staticmethod
    def _bracketed(target):
        """Return *target* as an unambiguous URI host (IPv6 literals get brackets)."""

        if ":" in target and not target.startswith("["):
            return "[%s]" % target
        return target

    def _tunnel_authority(self, target):
        return "%s:%d" % (self._bracketed(target), self._tunnel_port)

    def _connect_through_proxy(self):
        # Every vetted address is an equally valid CONNECT target, so a dual-stack
        # endpoint whose first address is unreachable *from the proxy* must fall through
        # to the rest exactly like the direct path does. A failed CONNECT leaves the
        # proxy socket unusable, so each attempt redials the proxy.
        last_error = None
        base_headers = dict(self._tunnel_headers)
        for target in self._tunnel_targets or [self._tunnel_host]:
            # Python 3.9 and 3.10 serialize the CONNECT request target verbatim, so a
            # bare IPv6 literal becomes an ambiguous "<addr>:<port>" authority that
            # strict proxies reject. 3.11+ bracket it themselves and leave an already
            # bracketed value untouched, so normalizing here is right on every version
            # this package supports.
            self._tunnel_host = self._bracketed(target)
            # 3.12+ also caches an authority in _tunnel_headers["Host"] when the tunnel
            # is configured. It must follow the address actually being CONNECTed, or a
            # strict proxy rejects the retry because the Host names the failed address.
            self._tunnel_headers = dict(base_headers)
            for name in list(self._tunnel_headers):
                if name.lower() == "host":
                    self._tunnel_headers[name] = self._tunnel_authority(target)
            try:
                self.sock = self._create_connection(
                    (self.host, self.port), self.timeout, self.source_address
                )
                self._tunnel()
                return
            except (OSError, UnicodeError) as exc:
                # UnicodeError: http.client encodes the tunnel host before sending it,
                # which is a reason to try the next address rather than abort outright.
                last_error = exc
                if self.sock is not None:
                    self.sock.close()
                    self.sock = None
        if last_error is None:
            raise OSError("cloud service URL has no connectable address")
        raise last_error


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler using pinned connections for every HTTPS request."""

    def https_open(self, req):
        # Python 3.12 folded ``check_hostname`` into the SSL context: ``HTTPSHandler`` no
        # longer keeps ``_check_hostname`` and ``HTTPSConnection`` no longer accepts the
        # keyword. Forward it only on the interpreters that still track it, so a single
        # code path works from 3.9 through 3.13.
        kwargs = {"context": self._context}
        check_hostname = getattr(self, "_check_hostname", None)
        if check_hostname is not None:
            kwargs["check_hostname"] = check_hostname
        return self.do_open(PinnedHTTPSConnection, req, **kwargs)


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
