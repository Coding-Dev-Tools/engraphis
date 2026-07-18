"""Cloud license enforcement (client side) — registration + short-lived signed leases.

This is the strongest enforcement an open-core client can carry. When ``ENGRAPHIS_CLOUD_URL``
is set, a paid key alone is NOT enough to unlock features: the device must *register* the
key with the vendor cloud, which returns a short-lived **lease** — an Ed25519-signed
statement (signed by the vendor private seed, verified here with the pinned public key)
binding {key_id, plan, features, machine_id, expiry}. Features require a currently-valid
lease, so:

  * an unregistered key gets nothing (registration is mandatory);
  * a revoked/refunded key stops working when its lease expires and the server refuses to
    renew (offline grace = the lease TTL, immediate when online);
  * a lease can't be transplanted to another machine (bound to ``machine_id``);
  * blocking the network only buys time until the current lease expires — it fails closed.

Honest limit: this raises the bar and makes revocation real, but a determined user can
still patch this open-source client on their own machine. Only features that *execute*
on the vendor server (e.g. the sync relay) are truly non-bypassable.

Online-only enforcement (product policy): paid features ALWAYS require a live lease. The
client resolves a server URL from ``ENGRAPHIS_CLOUD_URL`` -> the key's signed ``cloud_url``
-> the built-in vendor relay (``settings.relay_url``), and fails closed if none resolves.
There is deliberately no offline unlock path for Pro/Team.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import math
import os
import tempfile
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

_LEASE_PREFIX = "ENGRLS1"
_JSON_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    # Cloudflare rejects Python urllib's default signature with error 1010.
    "User-Agent": "Engraphis/1.0 (+https://engraphis.com)",
}


class Revoked(Exception):
    """The license server explicitly DENIED this key (HTTP 402/403) — it is revoked,
    expired, refunded, or over its seat cap.

    Distinct from a network failure (``register`` returns ``None``): a denial is an
    authoritative server decision that must propagate immediately, while an unreachable
    server falls back to the cached lease (offline grace). ``revalidate`` and ``gate``
    treat a ``Revoked`` as fail-closed and an offline result as grace, respectively."""


def _state_dir() -> Path:
    """Base dir for machine-id + lease state; ``ENGRAPHIS_STATE_DIR`` relocates it onto a
    persistent writable volume (Docker) so device binding survives redeploys."""
    base = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    return Path(base) if base else (Path.home() / ".engraphis")


_DIR = _state_dir()
_LEASE_FILE = _DIR / "lease.sig"
_MACHINE_ID_FILE = _DIR / "machine_id"
_REGISTER_TIMEOUT = 6.0

logger = logging.getLogger("engraphis")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_cloud_base_url(value: str) -> str:
    """Require HTTPS for remote cloud endpoints; allow HTTP only on loopback."""
    parts = urlsplit(str(value or "").strip())
    scheme = parts.scheme.lower()
    if scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("license server URL must be an absolute http(s) URL")
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("license server URL has an invalid port") from exc
    if parts.username is not None or parts.password is not None:
        raise ValueError("license server URL must not contain embedded credentials")
    if "\\" in parts.netloc or any(char.isspace() for char in parts.netloc):
        raise ValueError("license server URL contains an invalid host")
    if parts.query or parts.fragment:
        raise ValueError("license server URL must not contain a query string or fragment")
    if scheme != "https" and not _is_loopback_host(parts.hostname.lower()):
        raise ValueError("license server URL must use HTTPS unless it targets loopback")
    return urlunsplit((scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


#: Process-stable device-id cache, keyed by the resolved machine-id file path
#: (so tests that repoint _MACHINE_ID_FILE still get isolated ids). This is the
#: real fix for the silent-trial-death bug: even if the id can NEVER be persisted
#: (read-only/ephemeral container home, full disk, locked-down account), every
#: call within a run returns the SAME id — so the trial HMAC verifies and leases
#: bind consistently instead of churning a fresh uuid on every call.
_machine_id_cache: dict = {}
# Serializes first-run id generation so concurrent threads don't each mint a device id.
_machine_id_lock = threading.Lock()


def cloud_url() -> str:
    return os.environ.get("ENGRAPHIS_CLOUD_URL", "").strip().rstrip("/")


def machine_id() -> str:
    """Stable per-device id (random, created once). Binds a lease to this machine.

    Guarantees process-stability even when the id cannot be persisted: the value is
    cached in-memory (keyed by the machine-id file path) so it never changes within a
    run. Without this, an unwritable home made every call return a fresh uuid, which
    silently broke the local free trial (its HMAC is machine-bound) and churned cloud
    leases/seat counts. A persistence failure is logged once, not swallowed silently."""
    key = str(_MACHINE_ID_FILE)
    cached = _machine_id_cache.get(key)
    if cached:
        return cached
    with _machine_id_lock:
        # Re-check under the lock: another thread may have generated + cached the id while
        # we waited, so we don't mint a second one (which would burn an extra Team seat).
        cached = _machine_id_cache.get(key)
        if cached:
            return cached
        try:
            mid = _MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
            if mid:
                _machine_id_cache[key] = mid
                return mid
        except OSError:
            pass
        mid = uuid.uuid4().hex
        try:
            _MACHINE_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            # Publish a fully-written private file with an atomic create-if-absent link.
            # Creating the destination first and then writing it exposes an empty file to
            # a competing process, which can make that process cache a different id.
            fd, temp_name = tempfile.mkstemp(
                prefix=".machine_id.", dir=str(_MACHINE_ID_FILE.parent))
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fd = -1
                    fh.write(mid)
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    os.link(str(temp_path), str(_MACHINE_ID_FILE))
                except FileExistsError:
                    existing = _MACHINE_ID_FILE.read_text(encoding="utf-8").strip()
                    if existing:
                        mid = existing
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            try:
                os.chmod(_MACHINE_ID_FILE, 0o600)
            except OSError:
                pass
        except OSError as exc:
            logger.warning(
                "machine_id: could not persist device id to %s (%s); using an in-process "
                "id for this run. Trial and cloud-lease binding need a writable home "
                "directory — mount a persistent volume for %s (or set HOME to a writable "
                "path).",
                _MACHINE_ID_FILE, exc, _MACHINE_ID_FILE.parent)
        _machine_id_cache[key] = mid   # stable for the rest of the process regardless
        return mid


# ── lease token (same ENGR1-style envelope, distinct prefix) ─────────────────────────

def compose_lease(payload: dict, secret: bytes) -> str:
    """Vendor-side: sign a lease payload. (Server imports this.)"""
    from engraphis.licensing import _b64u_encode, ed25519_sign
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = ed25519_sign(secret, body)
    return "%s.%s.%s" % (_LEASE_PREFIX, _b64u_encode(body), _b64u_encode(sig))


def verify_lease(token: str, *, now: Optional[float] = None) -> dict:
    """Verify a lease against the pinned vendor public key. Raise LicenseError if bad.

    Checks the signature and that the lease has not expired (using the monotonic clock,
    so a rolled-back system clock cannot resurrect an expired lease)."""
    from engraphis.licensing import (
        LicenseError, _b64u_decode, ed25519_verify, vendor_public_key, _monotonic_now,
    )
    token = (token or "").strip()
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _LEASE_PREFIX:
        raise LicenseError("not a lease token")
    try:
        body = _b64u_decode(parts[1])
        sig = _b64u_decode(parts[2])
    except Exception:
        raise LicenseError("lease is not valid base64url")
    if not ed25519_verify(vendor_public_key(), body, sig):
        raise LicenseError("lease signature is invalid (tampered or wrong vendor key)")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LicenseError("lease payload is not valid JSON")
    # A signed body that decodes to valid-but-non-dict JSON ("5", "null", "[]") would make
    # the .get() below raise AttributeError instead of the LicenseError this function
    # documents. Every current caller wraps this in a broad `except Exception` and so
    # still fails closed, but a future caller catching LicenseError specifically would
    # not — mirror parse_key's isinstance guard rather than rely on that.
    if not isinstance(payload, dict):
        raise LicenseError("lease payload is not a JSON object")
    exp = payload.get("expires")
    now = _monotonic_now() if now is None else now
    if exp is None:
        raise LicenseError("lease has expired")
    try:
        exp_f = float(exp)
    except (TypeError, ValueError):  # non-numeric "expires" — unusable, not a 500
        raise LicenseError("lease expiry is not a number")
    # NaN and Infinity must be rejected EXPLICITLY, not left to the comparison below:
    # `now > nan` and `now > inf` are both False, so either value would sail past the
    # expiry check and yield a lease that NEVER expires — the one fail-OPEN in this
    # function. json.loads accepts a bare `NaN`/`Infinity` literal by default, and
    # float("inf") accepts the string form, so both are reachable from a payload.
    if not math.isfinite(exp_f):
        raise LicenseError("lease expiry is not a finite number")
    if now > exp_f:
        raise LicenseError("lease has expired")
    return payload


# ── local lease storage ──────────────────────────────────────────────────────────────

def _read_lease() -> str:
    try:
        return _LEASE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _write_lease(token: str) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = _LEASE_FILE.with_name(_LEASE_FILE.name + ".tmp")
        tmp.write_text(token, encoding="utf-8")
        os.replace(tmp, _LEASE_FILE)
        os.chmod(_LEASE_FILE, 0o600)
    except OSError:
        pass


def _valid_lease_for(key_id: str, mid: str) -> Optional[dict]:
    token = _read_lease()
    if not token:
        return None
    try:
        p = verify_lease(token)
    except Exception:
        return None
    if p.get("key_id") == key_id and p.get("machine_id") == mid:
        return p
    return None


def _delete_lease() -> None:
    """Remove the cached lease so the next ``gate`` call must re-register (and, if the
    key was revoked, be denied). Best-effort, never raises."""
    try:
        _LEASE_FILE.unlink()
    except OSError:
        pass


# ── registration (phone home) ────────────────────────────────────────────────────────

def register(base_url: str, key: str, mid: str, *, timeout: float = _REGISTER_TIMEOUT
             ) -> Optional[str]:
    """POST the key to the cloud; return a signed lease token, or None on a failure.

    Distinguishes a server DENIAL from being offline — the one piece of information the
    revocation path needs: a 402/403 (invalid, expired, revoked, or seat-limited) RAISES
    :class:`Revoked` so the caller fails closed immediately; a network error returns
    ``None`` so the caller can fall back to the cached lease (offline grace). Other HTTP
    statuses (e.g. a transient 5xx) also return ``None`` — no lease was minted, but the
    server did not definitively deny the key."""
    try:
        base = validate_cloud_base_url(base_url)
    except ValueError:
        logger.warning("license registration blocked: invalid service URL")
        return None
    url = base + "/license/v1/register"
    data = json.dumps({"key": key, "machine_id": mid}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers=_JSON_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("lease") or None
    except urllib.error.HTTPError as exc:
        if exc.code in (402, 403):
            raise Revoked("license denied by the server (HTTP %d)" % exc.code)
        return None  # transient/other HTTP status — no lease, but not a denial either
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None  # offline / unreachable


_INVITE_TIMEOUT = 10.0


def send_team_invite(base_url: str, key: str, to: str, name: str, role: str,
                     invited_by: str, *, dashboard_url: str = "",
                     timeout: float = _INVITE_TIMEOUT) -> Tuple[bool, str]:
    """POST a team-invite request to the vendor relay's ``/license/v1/team-invite``.

    Used by a self-hosted Team dashboard (``routes.v2_team.add_user``) that has no
    email delivery of its own configured — the vendor relay sends the notification
    through ITS configured mail provider instead, gated server-side by *key*
    actually carrying the ``team`` feature (see
    ``inspector.license_cloud.team_invite``). Returns ``(sent, reason)``: on any
    failure (network, 4xx/5xx, bad JSON) returns ``(False, <reason>)`` and never
    raises — the caller already has a working, created account regardless of
    whether the notification goes out, so a relay hiccup must never look like a
    bigger failure than it is.
    """
    try:
        base = validate_cloud_base_url(base_url)
    except ValueError:
        return False, "relay URL must use HTTPS (except loopback) and contain no credentials"
    url = base + "/license/v1/team-invite"
    data = json.dumps({"key": key, "to": to, "name": name, "role": role,
                       "invited_by": invited_by, "dashboard_url": dashboard_url}
                      ).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers=_JSON_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("sent")), ""
    except urllib.error.HTTPError as exc:
        if exc.code == 402:
            return False, "an active Team license is required for relayed invites"
        if exc.code == 429:
            # Two different 429s share this route. The short per-IP burst gate in front
            # of the relay's Ed25519 verify sends Retry-After; the per-key DAILY cap does
            # not. Without this branch an admin who tripped the 60-second gate is told to
            # come back tomorrow, and gives up on an invite that would work immediately.
            if exc.headers is not None and exc.headers.get("Retry-After"):
                return False, "relay is rate-limiting invites; retry shortly"
            return False, "daily relayed-invite limit reached; retry tomorrow"
        return False, "relay rejected invite (HTTP %d)" % exc.code
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return False, "relay is unreachable; check ENGRAPHIS_RELAY_URL and the network"


def request_trial_key(base_url: str, mid: str, plan: str = "team", email: str = "", *,
                      timeout: float = _INVITE_TIMEOUT) -> Tuple[Optional[str], str, bool]:
    """POST to the vendor relay's self-serve ``/license/v1/start-trial`` and return
    ``(key, reason, pending)``.

    Since 2026-07-14 the relay no longer issues a key synchronously from this call —
    machine_id alone was a trivially-resettable trial-abuse vector (delete one local
    file, get infinite "free" trials; see ``inspector.license_cloud``'s module comment).
    A real key now requires the caller to open a one-time magic link sent to *email*
    (redeemed server-side, not by this client — there is no matching "confirm" call
    here to make: the link is meant to be clicked from the inbox, not fetched by this
    process). So the normal, successful outcome of THIS call is ``(None, <a "check
    your email" message>, True)`` — ``key`` stays non-None only for compatibility with
    a relay that still short-circuits. ``pending`` is False on every other outcome
    (already-used-device 409, bad request, or a network/relay failure): those are hard
    stops, not "come back later". Used by :func:`engraphis.licensing.start_trial`
    (pro) and :func:`~engraphis.licensing.start_team_trial` (team)."""
    try:
        base = validate_cloud_base_url(base_url)
    except ValueError:
        return None, ("relay URL must use HTTPS (except loopback) and contain no "
                      "credentials"), False
    url = base + "/license/v1/start-trial"
    data = json.dumps({"machine_id": mid, "email": email, "plan": plan}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers=_JSON_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            body = json.loads(resp.read().decode("utf-8"))
        key = body.get("key")
        if key:
            return key, "", False
        if body.get("pending"):
            return None, "check your email to confirm and activate the trial", True
        return None, "relay returned no key", False
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            return None, "the free trial has already been used on this device", False
        if exc.code == 429:
            return None, "too many trial requests; try again later", False
        return None, "trial relay rejected the request (HTTP %d)" % exc.code, False
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None, ("trial relay is unreachable; check ENGRAPHIS_RELAY_URL and the "
                      "network"), False


def request_team_trial_key(base_url: str, mid: str, email: str = "", *,
                           timeout: float = _INVITE_TIMEOUT
                           ) -> Tuple[Optional[str], str, bool]:
    """Backward-compat wrapper for :func:`request_trial_key` with ``plan="team"``."""
    return request_trial_key(base_url, mid, plan="team", email=email, timeout=timeout)


def gate(lic, key_material: str, *, base_url: Optional[str] = None) -> Tuple[bool, str]:
    """Decide whether a validly-signed paid key may unlock features on this device.

    Returns ``(allowed, reason)``. ``base_url`` (the caller usually passes the env
    override or the URL signed into the key) takes precedence over ``ENGRAPHIS_CLOUD_URL``.
    **With no server resolved at all this fails CLOSED** — there is deliberately no
    offline path to paid features (the caller, ``licensing._cloud_gate``, resolves a
    default relay URL and also fails closed, so this only bites a deliberately blanked
    config — defense in depth). In cloud mode, every cache refresh contacts the server:
    an authoritative denial fails closed immediately, while a transient network failure
    may use an existing unexpired lease as offline grace. Without such a lease, failure
    to register fails closed."""
    base = (base_url or "").strip().rstrip("/") or cloud_url()
    if not base:
        # Online-only: no server to verify against ⇒ no offline path to paid features.
        # (The caller, licensing._cloud_gate, already resolves a default relay URL, so
        # this only bites a deliberately blanked config — defense in depth. Fail closed.)
        return False, ("server-side license verification is required but no license "
                       "server is configured")
    try:
        base = validate_cloud_base_url(base)
    except ValueError as exc:
        # A malformed/insecure endpoint is a configuration error, not an offline
        # condition. Do not silently honor a cached lease, and do not echo the original
        # URL because it may contain embedded credentials.
        return False, "cloud license verification is blocked: %s" % exc
    mid = machine_id()
    cached = _valid_lease_for(lic.key_id, mid)
    try:
        token = register(base, key_material, mid)
    except Revoked as exc:
        _delete_lease()
        return False, str(exc)
    if token:
        try:
            payload = verify_lease(token)
        except Exception:
            payload = None
        if (payload and payload.get("key_id") == lic.key_id
                and payload.get("machine_id") == mid):
            _write_lease(token)
            return True, ""
    if cached is not None:
        return True, ""
    return False, ("cloud license verification failed — could not reach license server at %s "
                   "(offline or network error)" % base)


def revalidate(lic, key_material: str, *, base_url: Optional[str] = None) -> str:
    """Refresh an active cloud lease without sacrificing offline grace.

    Unlike :func:`gate`, this path always contacts the server. An authoritative denial
    deletes the cached lease and invalidates the process cache immediately; a network or
    transient server failure leaves the existing lease untouched until its signed expiry.
    """
    from engraphis import licensing

    base = (base_url or "").strip().rstrip("/") or cloud_url()
    if not base:
        return "offline"
    try:
        base = validate_cloud_base_url(base)
    except ValueError:
        licensing.invalidate_cache()
        return "offline"
    mid = machine_id()
    try:
        token = register(base, key_material, mid)
    except Revoked:
        _delete_lease()
        licensing.invalidate_cache()
        return "revoked"
    if not token:
        return "offline"
    try:
        payload = verify_lease(token)
    except Exception:
        return "offline"
    if payload.get("key_id") != lic.key_id or payload.get("machine_id") != mid:
        return "offline"
    _write_lease(token)
    return "ok"
