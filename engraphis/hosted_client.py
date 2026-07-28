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
import sys
import time
import urllib.request
from typing import Optional
from urllib.parse import urlsplit, urlunsplit


TRIAL_DAYS = 3
TRIAL_SECONDS = 3 * 24 * 60 * 60
MAX_HOSTED_ACCOUNT_GRACE_SECONDS = 24 * 60 * 60
# Compatibility alias for clients released before the public/private boundary was made
# explicit. The public local core is never paywalled; this duration belongs to private
# hosted account continuity.
MAX_LOCAL_WRITE_GRACE_SECONDS = MAX_HOSTED_ACCOUNT_GRACE_SECONDS

# A credential-bearing cloud connection must never inherit "block forever".  urllib hands
# ``socket._GLOBAL_DEFAULT_TIMEOUT`` to the connection whenever a caller omits ``timeout=``,
# so substitute a bounded default rather than letting a customer's agent hang on launch day.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 15.0
# One vetted address must not be able to spend the whole budget and leave nothing for the
# rest, but every attempt still gets a usable floor so a slow first hop is not aborted.
MIN_ATTEMPT_TIMEOUT_SECONDS = 0.5

# The hosted dashboard and the commercial account portal are separate surfaces.
# Upgrade/connect actions must land on the authenticated control-plane portal; the
# dashboard host does not serve its own ``/account`` route.
DEFAULT_CLOUD_URL = "https://api.engraphis.com/account"
_DEFAULT_CHECKOUT_URLS = {
    ("pro", "monthly"): (
        "https://api.engraphis.com/account?plan=pro&interval=monthly#billing"
    ),
    ("pro", "annual"): (
        "https://api.engraphis.com/account?plan=pro&interval=annual#billing"
    ),
    ("team", "monthly"): (
        "https://api.engraphis.com/account?plan=team&interval=monthly#billing"
    ),
    ("team", "annual"): (
        "https://api.engraphis.com/account?plan=team&interval=annual#billing"
    ),
}

_REQUIRED_PLAN = {
    "analytics": "pro",
    "automation": "pro",
    "consolidation": "pro",
    "dreaming": "pro",
    "export": "pro",
    "sync": "pro",
    "team": "team",
    # Team-only capabilities named in commercial_manifest.json.  Without explicit entries
    # these fell through to the "pro" default, so a Team capability would tell a customer
    # who already holds Pro to buy Pro again.
    "hosted_team_audit_export": "team",
    "hosted_scoped_agent_tokens": "team",
    "hosted_multi_user_roles": "team",
}


class CloudUrlUnresolved(ValueError):
    """The cloud endpoint could not be resolved right now.

    This is a *reachability* failure, not a misconfiguration: an offline laptop and a
    broken resolver both land here.  It stays a ``ValueError`` so existing callers keep
    working, but lets a caller degrade an offline paying customer to a retryable
    "temporarily unreachable" instead of a permanent "your configuration is invalid".
    """


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


def _safe_target(value: str) -> str:
    """Return ``value`` only when it is an absolute https:// (or loopback http://) URL.

    These values are operator-supplied environment variables that end up interpolated
    into customer-facing copy and into the ``href`` the dashboard renders.  A missing
    scheme ("api.engraphis.com/account") silently resolved against the local dashboard
    origin and produced a 404 checkout button with no error; a ``javascript:`` value had
    only the dashboard's own scheme filter standing between it and an anchor.  Validate
    once, here, and degrade to the known-good destination rather than to a dead link.
    """

    text = str(value or "").strip()
    if not text:
        return ""
    # urlsplit normalizes some ASCII controls while parsing.  Returning the original
    # operator string after that would still put those controls in dashboard metadata
    # and customer-facing hrefs, so reject them before parsing instead of relying on a
    # browser-side sanitizer to repair server output.
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if (
        not parts.hostname
        # Upgrade/account destinations are rendered as links, not credential-bearing
        # transport endpoints.  Userinfo in an operator override would make browsers
        # send that credential to the destination on click (and can expose it in the
        # address bar or browser history), so it is never a safe hosted destination.
        or parts.username is not None
        or parts.password is not None
    ):
        return ""
    try:
        # Accessing ``port`` is validation: urlsplit accepts an invalid textual port
        # until this property is read.  Do not hand a dead/malformed URL to the
        # dashboard and call it a valid upgrade destination.
        parts.port
    except ValueError:
        return ""
    if parts.scheme == "https":
        return text
    # Loopback http is allowed so a self-hosted control plane can be pointed at locally.
    if parts.scheme == "http" and _is_loopback_host(parts.hostname):
        return text
    return ""


def upgrade_url(plan: Optional[str] = None, interval: Optional[str] = None) -> str:
    """Return the exact hosted checkout target for a plan and billing interval.

    The four defaults intentionally mirror ``commercial_manifest.json``. Operator
    overrides stay authoritative and are returned verbatim after validation; deployments
    that need distinct cadence-specific custom pages can expose those choices in their
    own account portal.
    """

    name = str(plan or "pro").strip().lower()
    name = "team" if name == "team" else "pro"
    cadence = str(interval or "monthly").strip().lower()
    cadence = cadence if cadence in ("monthly", "annual") else "monthly"
    if name == "team":
        value = (
            _safe_target(os.environ.get("ENGRAPHIS_TEAM_UPGRADE_URL", ""))
            or _safe_target(os.environ.get("ENGRAPHIS_UPGRADE_URL", ""))
        )
    else:
        value = (
            _safe_target(os.environ.get("ENGRAPHIS_PRO_UPGRADE_URL", ""))
            or _safe_target(os.environ.get("ENGRAPHIS_UPGRADE_URL", ""))
        )
    return value or _DEFAULT_CHECKOUT_URLS[(name, cadence)]


def account_url() -> str:
    """Return the plan-neutral hosted account URL.

    ``upgrade_url()`` is a *checkout* selector, not an account entry point: with no
    argument it resolves ``plan="pro"`` and therefore prefers
    ``ENGRAPHIS_PRO_UPGRADE_URL``. Wherever those are configured as distinct pages, a
    lapsed customer sent to "the account portal" would land on the Pro checkout — the
    wrong product offered to someone whose problem is a payment method on a subscription
    they already hold. Resolve the generic value directly instead, and fall back to the
    hosted account root rather than to either plan's page.
    """

    return _safe_target(os.environ.get("ENGRAPHIS_UPGRADE_URL", "")) or DEFAULT_CLOUD_URL


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
        raise CloudUrlUnresolved("cloud service URL could not be resolved") from None

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
        raise CloudUrlUnresolved("cloud service URL could not be resolved")
    return list(dict.fromkeys(addresses))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to a vetted address with original-host TLS checks."""

    def __init__(self, host, *args, **kwargs):
        super().__init__(host, *args, **kwargs)
        # ``http.client`` stores ``socket._GLOBAL_DEFAULT_TIMEOUT`` (or ``None``) when no
        # timeout was supplied, and both mean "block forever" at the socket layer.  Neither
        # is acceptable for a credential-bearing hosted call, so pin a bounded default.
        if not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool):
            self.timeout = DEFAULT_CONNECT_TIMEOUT_SECONDS
        self._tls_server_hostname = self.host
        self._tunnel_targets = []

    def _connect_deadline(self):
        """Return the monotonic instant by which every dial attempt must be finished."""

        return time.monotonic() + max(float(self.timeout), 0.0)

    def _attempt_timeout(self, deadline):
        """Share one caller-supplied budget across every vetted address.

        Handing each address the full timeout turns a 10s budget into N x 10s for a
        multi-homed endpoint that blackholes traffic -- and ``cloud_session`` dials while
        holding an exclusive cross-process refresh lock, so every other worker waits too.
        """

        return max(deadline - time.monotonic(), MIN_ATTEMPT_TIMEOUT_SECONDS)

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
        deadline = self._connect_deadline()
        for target in _validated_addresses(self.host):
            # Overriding connect() skips the sys.audit call in HTTPConnection.connect,
            # which would make every hosted request invisible to a host process auditing
            # or blocking outbound connections. Emit it per dial, naming the vetted
            # address actually opened rather than the hostname, so a hook sees the truth.
            sys.audit("http.client.connect", self, target, self.port)
            try:
                return self._create_connection(
                    (target, self.port), self._attempt_timeout(deadline),
                    self.source_address,
                )
            except OSError as exc:
                last_error = exc
                if time.monotonic() >= deadline:
                    break
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
        deadline = self._connect_deadline()
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
            # The socket opened here goes to the proxy, which is exactly what the stock
            # implementation audits before a tunnelled request, so report the proxy.
            sys.audit("http.client.connect", self, self.host, self.port)
            try:
                self.sock = self._create_connection(
                    (self.host, self.port), self._attempt_timeout(deadline),
                    self.source_address,
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
                if time.monotonic() >= deadline:
                    break
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
