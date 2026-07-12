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

Offline-only mode: with no ``ENGRAPHIS_CLOUD_URL`` set, this module is inert and a signed
key verifies locally as before — preserving the self-hosted, no-phone-home story.
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


def gate(lic, key_material: str) -> Tuple[bool, str]:
    """Decide whether a validly-signed paid key may unlock features on this device.

    Returns ``(allowed, reason)``. In offline-only mode (no ``ENGRAPHIS_CLOUD_URL``) always
    allows — the local signature is the gate. In cloud mode, requires a valid lease,
    fetching/renewing one by registering; fails closed if it can't."""
    base = cloud_url()
    if not base:
        return True, ""                              # offline-only mode: inert
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
