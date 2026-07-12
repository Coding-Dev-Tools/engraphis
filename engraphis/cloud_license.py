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

import json
import logging
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Optional, Tuple

_LEASE_PREFIX = "ENGRLS1"
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
#: Process-stable device-id cache, keyed by the resolved machine-id file path
#: (so tests that repoint _MACHINE_ID_FILE still get isolated ids). This is the
#: real fix for the silent-trial-death bug: even if the id can NEVER be persisted
#: (read-only/ephemeral container home, full disk, locked-down account), every
#: call within a run returns the SAME id — so the trial HMAC verifies and leases
#: bind consistently instead of churning a fresh uuid on every call.
_machine_id_cache: dict = {}


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
        _MACHINE_ID_FILE.write_text(mid, encoding="utf-8")
        try:
            os.chmod(_MACHINE_ID_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        logger.warning(
            "machine_id: could not persist device id to %s (%s); using an in-process id "
            "for this run. Trial and cloud-lease binding need a writable home directory "
            "— mount a persistent volume for %s (or set HOME to a writable path).",
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
    exp = payload.get("expires")
    now = _monotonic_now() if now is None else now
    if exp is None or now > float(exp):
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


# ── registration (phone home) ────────────────────────────────────────────────────────

def register(base_url: str, key: str, mid: str, *, timeout: float = _REGISTER_TIMEOUT
             ) -> Optional[str]:
    """POST the key to the cloud; return a signed lease token, or None on any failure/deny.

    A 402/403 (invalid, expired, revoked, or seat-limited) returns None — the caller then
    fails closed. Network errors also return None (fail closed once the old lease lapses)."""
    url = base_url.rstrip("/") + "/license/v1/register"
    data = json.dumps({"key": key, "machine_id": mid}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return body.get("lease") or None
    except (urllib.error.URLError, ValueError, TimeoutError, OSError):
        return None


_INVITE_TIMEOUT = 10.0


def send_team_invite(base_url: str, key: str, to: str, name: str, role: str,
                     invited_by: str, *, timeout: float = _INVITE_TIMEOUT) -> Tuple[bool, str]:
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
    url = base_url.rstrip("/") + "/license/v1/team-invite"
    data = json.dumps({"key": key, "to": to, "name": name, "role": role,
                       "invited_by": invited_by}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        return bool(body.get("sent")), ""
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return False, (detail or "relay returned HTTP %d" % exc.code)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
        return False, "relay unreachable: %s" % exc


def request_trial_key(base_url: str, mid: str, plan: str = "team", email: str = "", *,
                      timeout: float = _INVITE_TIMEOUT) -> Tuple[Optional[str], str]:
    """POST to the vendor relay's self-serve ``/license/v1/start-trial`` and return
    ``(signed_key, reason)`` for a one-time trial of ``plan`` ("pro" or "team"). *key* is
    ``None`` on any failure — already used on this device (409), network error, bad
    response — with *reason* explaining why. Used by :func:`engraphis.licensing.
    start_trial` (pro) and :func:`~engraphis.licensing.start_team_trial` (team): the trial
    is a REAL, independently verifiable vendor-signed key (never a client-only claim), so
    it satisfies the same server-side gates (/register, team_invite) every paid feature
    uses."""
    url = base_url.rstrip("/") + "/license/v1/start-trial"
    data = json.dumps({"machine_id": mid, "email": email, "plan": plan}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        key = body.get("key")
        return (key, "") if key else (None, "relay returned no key")
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", "")
        except Exception:
            detail = ""
        return None, (detail or "relay returned HTTP %d" % exc.code)
    except (urllib.error.URLError, ValueError, TimeoutError, OSError) as exc:
        return None, "relay unreachable: %s" % exc


def request_team_trial_key(base_url: str, mid: str, email: str = "", *,
                           timeout: float = _INVITE_TIMEOUT) -> Tuple[Optional[str], str]:
    """Backward-compat wrapper for :func:`request_trial_key` with ``plan="team"``."""
    return request_trial_key(base_url, mid, plan="team", email=email, timeout=timeout)


def gate(lic, key_material: str, *, base_url: Optional[str] = None) -> Tuple[bool, str]:
    """Decide whether a validly-signed paid key may unlock features on this device.

    Returns ``(allowed, reason)``. ``base_url`` (the caller usually passes the env
    override or the URL signed into the key) takes precedence over ``ENGRAPHIS_CLOUD_URL``.
    With no server at all this is inert (allow) — the caller (``licensing._cloud_gate``)
    is responsible for denying ``enforce: "cloud"`` keys before ever reaching that case.
    In cloud mode, requires a valid lease, fetching/renewing one by registering; fails
    closed if it can't."""
    base = (base_url or "").strip().rstrip("/") or cloud_url()
    if not base:
        # Online-only: no server to verify against ⇒ no offline path to paid features.
        # (The caller, licensing._cloud_gate, already resolves a default relay URL, so
        # this only bites a deliberately blanked config — defense in depth. Fail closed.)
        return False, ("server-side license verification is required but no license "
                       "server is configured")
    mid = machine_id()
    if _valid_lease_for(lic.key_id, mid) is not None:
        return True, ""                              # within an unexpired lease window
    token = register(base, key_material, mid)        # (re)register / renew
    if token:
        try:
            p = verify_lease(token)
        except Exception:
            p = None
        if p and p.get("key_id") == lic.key_id and p.get("machine_id") == mid:
            _write_lease(token)
            return True, ""
    return False, ("cloud license verification failed — this license could not be "
                   "validated with %s (revoked, seat limit, or offline past the lease "
                   "window)" % base)
