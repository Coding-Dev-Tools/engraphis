"""Offline license verification for the Engraphis paid tiers (commercial/open-core layer).

Open-core with signed keys: the free core is complete and Apache-2.0; Pro/Team features
activate with an ``ENGR1.<payload>.<signature>`` key whose JSON payload is Ed25519-signed
by the vendor.

Ed25519 is implemented here from RFC 8032 (verify *and* sign — the vendor CLI
``scripts/license_admin.py`` reuses the same math; the private key itself never ships).
It is the reference algorithm, exercised against the RFC's own test vectors in
``tests/test_licensing.py``. Speed is irrelevant at one verify per process start.

Key resolution order: ``ENGRAPHIS_LICENSE_KEY`` env var, then ``~/.engraphis/license.key``.
Feature gates call :func:`has_feature` / :func:`require_feature`; the free tier is the
absence of a license, never an error.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import sys
import threading
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ── Ed25519 (RFC 8032, ref. implementation style; verify-grade, not constant-time —
#    fine here: signing happens only vendor-side, verifying uses only public data) ──────

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _recover_x(y: int, sign: int) -> int:
    if y >= _P:
        raise ValueError("invalid point encoding")
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    if x2 == 0:
        if sign:
            raise ValueError("invalid point encoding")
        return 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _I % _P
    if (x * x - x2) % _P != 0:
        raise ValueError("invalid point encoding")
    if (x & 1) != sign:
        x = _P - x
    return x


# Points are extended homogeneous coordinates (X, Y, Z, T), RFC 8032 §5.1.4.
def _pt_add(p, q):
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _pt_mul(p, s: int):
    q = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            q = _pt_add(q, p)
        p = _pt_add(p, p)
        s >>= 1
    return q


def _pt_equal(p, q) -> bool:
    return ((p[0] * q[2] - q[0] * p[2]) % _P == 0 and
            (p[1] * q[2] - q[1] * p[2]) % _P == 0)


_BY = 4 * pow(5, _P - 2, _P) % _P
_BX = _recover_x(_BY, 0)
_B = (_BX, _BY, 1, _BX * _BY % _P)


def _pt_encode(p) -> bytes:
    zinv = pow(p[2], _P - 2, _P)
    x, y = p[0] * zinv % _P, p[1] * zinv % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _pt_decode(raw: bytes):
    if len(raw) != 32:
        raise ValueError("invalid point encoding")
    val = int.from_bytes(raw, "little")
    sign = val >> 255
    y = val & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return (x, y, 1, x * y % _P)


def ed25519_public_key(secret: bytes) -> bytes:
    """Derive the 32-byte public key from a 32-byte secret seed."""
    if len(secret) != 32:
        raise ValueError("secret key must be 32 bytes")
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return _pt_encode(_pt_mul(_B, a))


def ed25519_sign(secret: bytes, message: bytes) -> bytes:
    """RFC 8032 sign (vendor-side only; used by scripts/license_admin.py)."""
    h = _sha512(secret)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:]
    pub = _pt_encode(_pt_mul(_B, a))
    r = int.from_bytes(_sha512(prefix + message), "little") % _L
    rp = _pt_encode(_pt_mul(_B, r))
    k = int.from_bytes(_sha512(rp + pub + message), "little") % _L
    s = (r + k * a) % _L
    return rp + s.to_bytes(32, "little")


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 verify. Returns False (never raises) on any malformed input."""
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        a = _pt_decode(public)
        rp = _pt_decode(signature[:32])
    except ValueError:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = int.from_bytes(_sha512(signature[:32] + public + message), "little") % _L
    return _pt_equal(_pt_mul(_B, s), _pt_add(rp, _pt_mul(a, k)))


# ── feature registry (keep the free/paid line stable) ──────────────────────────────

#: Paid features that exist today, with the one-line description the UI shows.
FEATURES: dict = {
    "analytics": "Analytics — growth, retention distribution, decay forecast, entity insights, shareable HTML report",
    "export": "Compliance export — signed, checksummed bi-temporal workspace bundle (memories + audit)",
    "automation": "Automated maintenance — scheduled consolidation + retention policies that keep the store clean on autopilot",
    "sync": "Cloud sync — multi-device & team sync of your memory store with deterministic conflict resolution (bi-temporal merge, no conflict copies, no lost notes)",
    "team": "Team mode — multi-user dashboard with logins, roles, and per-seat management",
}

#: What each plan unlocks. Unknown feature names in a key are carried but inert.
#: ``sync`` is the flagship Pro upsell (individual multi-device); Team inherits it
#: and adds multi-user shared-workspace sync on top.
PLAN_FEATURES: dict = {
    "pro": frozenset({"analytics", "export", "automation", "sync"}),
    "team": frozenset({"analytics", "export", "automation", "team", "sync"}),
}

#: Where to buy — shown by the dashboard's license panel and 402 error messages.
#: Pro and Team are distinct products so fulfillment maps cleanly to a plan; each
#: URL is independently env-overridable. ``ENGRAPHIS_UPGRADE_URL`` remains the
#: general/Pro default for backward compatibility.
DEFAULT_UPGRADE_URL = "https://buy.polar.sh/polar_cl_n6CR3ERqOus2VUhRrGrsRUqOB8yjDTeEU7p1r3CRrae"
DEFAULT_PRO_UPGRADE_URL = DEFAULT_UPGRADE_URL
DEFAULT_TEAM_UPGRADE_URL = DEFAULT_UPGRADE_URL
#: Informational landing page shown instead of the live checkout until the vendor side
#: (signer rotation, Railway env, Polar/Resend wiring) is fully live. Without this gate a
#: free 1.0.0 launch's "Buy Pro"/"Buy Team" button hits a LIVE Polar checkout and can
#: charge a customer before a license can actually be fulfilled.
DEFAULT_COMING_SOON_URL = "https://engraphis.com/"


def _paid_available() -> bool:
    """Master switch for the free-vs-paid launch split.

    False (the default) routes upgrade links to the informational coming-soon page
    instead of the live Polar checkout, so enabling real charges is an explicit,
    reviewed step (``ENGRAPHIS_PAID_AVAILABLE=1``) rather than an accidental default."""
    return os.environ.get("ENGRAPHIS_PAID_AVAILABLE", "").strip().lower() in (
        "1", "true", "yes")


def upgrade_url(plan: Optional[str] = None) -> str:
    """The URL a user should visit to buy ``plan`` (defaults to the Pro/general link).

    Env-configurable and never empty: ``ENGRAPHIS_TEAM_UPGRADE_URL`` for Team,
    ``ENGRAPHIS_PRO_UPGRADE_URL`` (or the legacy ``ENGRAPHIS_UPGRADE_URL``) for Pro. An
    explicit override always wins; otherwise this routes to the coming-soon page unless
    ``ENGRAPHIS_PAID_AVAILABLE=1`` (see :func:`_paid_available`)."""
    if (plan or "").lower() == "team":
        override = (os.environ.get("ENGRAPHIS_TEAM_UPGRADE_URL", "").strip()
                    or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip())
        if override:
            return override
        return DEFAULT_TEAM_UPGRADE_URL if _paid_available() else DEFAULT_COMING_SOON_URL
    override = (os.environ.get("ENGRAPHIS_PRO_UPGRADE_URL", "").strip()
                or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip())
    if override:
        return override
    return DEFAULT_PRO_UPGRADE_URL if _paid_available() else DEFAULT_COMING_SOON_URL

_KEY_PREFIX = "ENGR1"
# Pinned Ed25519 verifier (32-byte public half) derived from the production Railway signing
# seed during the 2026-07-22 release ceremony. The production registry inventory contained
# no issued keys, so this was a clean rotation with no compatibility verifier or reissue.
# The private seed never ships in this repo; production keeps it only in the vendor secret
# store and an encrypted recovery backup. Anyone with only this repository cannot forge a
# valid key. Future rotations follow the audited ceremony in docs/COMMERCIAL_OPERATIONS.md.
_VENDOR_PUBKEY_HEX = "77d0f9e4637bc322e494c0073b03266009a6140c7e1b99d0f47b827d4ece6d83"
# Previous production verify keys live here only during an audited rotation window.
# New issuance always uses ``_VENDOR_PUBKEY_HEX``; remove retired entries after every
# customer has received a replacement and the announced grace period has elapsed.
_PREVIOUS_VENDOR_PUBKEY_HEXES = ()

# Source-controlled proof that the trusted-machine ceremony, inventory, and verifier pin
# were independently reviewed before production issuance was enabled.
VENDOR_SIGNER_RELEASE_READY = True
# Frozen fingerprint of the OLD, known-compromised dev keypair. Kept as a sentinel so
# is_default_vendor_key() / production_warnings() can flag it if anyone ever re-pins it.
# Its private half does NOT ship in this repo (`.secrets/` is gitignored), but it was
# exposed in dev boxes / agent sessions and must never be the active key for selling.
_DEV_VENDOR_PUBKEY_HEX = "4722dc145d7b988f6a2513e750e367beb2dd75a68a208c8546b1fbb61c862b7e"

def _state_dir() -> Path:
    """Base dir for local license / trial / clock-anchor state.

    ``ENGRAPHIS_STATE_DIR`` relocates it onto a persistent, writable volume (e.g.
    ``/data/.engraphis`` in Docker) so an activated key and the one-time trial survive
    container recreation; defaults to ``~/.engraphis`` for local/desktop use."""
    base = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    return Path(base) if base else (Path.home() / ".engraphis")


_STATE_DIR = _state_dir()
_LICENSE_FILE = _STATE_DIR / "license.key"

#: Advisory, NON-granting marker that this device already consumed its one-time free
#: trial. Purely a UI hint so the dashboard can stop offering the "start trial" button;
#: it confers no entitlement. The trial is a real server-issued key and the server is the
#: authoritative one-trial-per-device gate, so deleting this file cannot re-arm a trial.
_TRIAL_STAMP = _STATE_DIR / "trial_used.json"


def _trial_used_locally() -> bool:
    try:
        return bool(json.loads(_TRIAL_STAMP.read_text(encoding="utf-8")).get("used"))
    except (OSError, ValueError):
        return False


def _mark_trial_used(*, now: Optional[float] = None) -> None:
    """Best-effort advisory stamp (see :data:`_TRIAL_STAMP`). Never raises."""
    try:
        _TRIAL_STAMP.parent.mkdir(parents=True, exist_ok=True)
        _TRIAL_STAMP.write_text(
            json.dumps({"used": True, "at": int(time.time() if now is None else now)}),
            encoding="utf-8")
    except OSError:
        pass

#: One-time self-serve free trial length (days). The trial is now a REAL, short-lived,
#: vendor-signed key fetched from the license server (see :func:`start_trial` /
#: :func:`start_team_trial`), NOT a local grant — so it is verified by the same
#: server-side gate as a purchase and cannot be forged offline. One trial per device,
#: enforced server-side (``inspector/license_cloud.py``).
TRIAL_DAYS = 3
_TRIAL_FILE = _STATE_DIR / "trial.json"

#: Trial-used tombstones. ``trial.json`` above is the *grant* (the active window) and
#: lives in the single state dir — but when that dir alone also recorded that the trial
#: was consumed, one ``rm -rf ~/.engraphis`` was an infinite-Pro reset loop. Starting a
#: trial therefore also drops a marker in each independent location below, and the trial
#: counts as USED when ANY of them exists (presence alone counts: we are the only writer,
#: so an emptied/corrupted marker still blocks a re-grant rather than enabling one).
#: Honest limit (open-core): someone who hunts down every marker — or edits this source —
#: can still reset; these close the casual one-command reset. The truly non-bypassable
#: gates remain the vendor-hosted cloud/relay checks.
_TOMBSTONE_DIRS_OVERRIDE: Optional[list] = None   # tests re-route; None = real locations


def _trial_tombstone_files() -> list:
    """Every location that may carry the trial-used marker (first entry is preferred)."""
    if _TOMBSTONE_DIRS_OVERRIDE is not None:
        return [Path(d) / "trial.stamp" for d in _TOMBSTONE_DIRS_OVERRIDE]
    dirs = []
    for env in ("LOCALAPPDATA", "APPDATA", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        base = os.environ.get(env, "").strip()
        if base:
            dirs.append(Path(base) / "engraphis")
    home = Path.home()
    dirs.append(home / ".cache" / "engraphis")
    if _STATE_DIR != home / ".engraphis":  # relocated state dir: stamp the default too
        dirs.append(home / ".engraphis")
    seen, out = set(), []
    for d in dirs:
        if str(d) not in seen:
            seen.add(str(d))
            out.append(d / "trial.stamp")
    return out


def _trial_used_elsewhere() -> bool:
    """True if any tombstone marks the one-time trial as already consumed."""
    for path in _trial_tombstone_files():
        try:
            if path.exists():
                return True
        except OSError:
            continue
    return False


_MONOTONIC_FILE = _STATE_DIR / ".clock_anchor"


def _monotonic_now() -> float:
    """``max(system clock, highest time ever seen)`` — never moves backward across calls.

    Reads the anchor, returns the greater of it and ``time.time()``, and advances the
    anchor to that value. A clock rolled into the past therefore cannot reduce measured
    elapsed time for expiry checks."""
    now = time.time()
    anchor = now
    try:
        anchor = max(now, float(_MONOTONIC_FILE.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        pass
    if anchor >= now:  # persist the high-water mark (best-effort)
        try:
            _MONOTONIC_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = _MONOTONIC_FILE.with_name(_MONOTONIC_FILE.name + ".tmp")
            tmp.write_text(repr(anchor), encoding="utf-8")  # full float precision
            os.replace(tmp, _MONOTONIC_FILE)
        except OSError:
            pass
    return anchor


def _trial_hmac_key() -> bytes:
    """Machine-tied key for signing trial state, so ``trial.json`` can't be hand-edited
    to extend a trial or transplanted to another machine. Derived from the pinned vendor
    public key + this device's machine id (both stable, neither secret) — this raises the
    bar against casual tampering; it is not unforgeable in an open-source client."""
    key = _VENDOR_PUBKEY_HEX.encode("ascii")
    try:
        from engraphis.cloud_license import machine_id
        key += machine_id().encode("ascii")
    except Exception:
        pass
    return hashlib.sha256(key).digest()


def _sign_trial(payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hmac.new(_trial_hmac_key(), body, "sha256").hexdigest()


class LicenseError(Exception):
    """Invalid, tampered, or expired license key. Message is safe to surface.

    When raised by :func:`require_feature`, ``feature`` names the locked feature so
    HTTP layers can render a structured 402 (feature / tier_required / upgrade_url)
    without parsing the message.
    """

    def __init__(self, message: str, *, feature: Optional[str] = None):
        super().__init__(message)
        self.feature = feature


@dataclass(frozen=True)
class License:
    """A verified license. ``License.free()`` is the tier-0 sentinel, not an error."""

    plan: str = "free"
    email: str = ""
    seats: int = 1
    issued: Optional[float] = None
    expires: Optional[float] = None
    features: frozenset = field(default_factory=frozenset)
    key_id: str = ""  # short fingerprint for support/display; never the key itself
    #: Fingerprint of the Ed25519 public key that verified this license. New keys carry
    #: the same value in their signed payload; legacy keys derive it from the verifier
    #: that accepted them so a pre-rotation inventory can still identify their signer.
    signing_key_id: str = ""
    is_trial: bool = False  # True when this is a time-boxed server-issued trial key
    #: Historical signed policy marker. Every paid/trial key now requires a live
    #: server-side lease regardless of this value; it remains for key compatibility.
    enforce: str = ""
    #: License-server URL baked into the key at issuance — also signed/unforgeable.
    cloud_url: str = ""
    #: Optional vendor-side identifiers used only for server revocation lookups. They are
    #: signed into auto-issued keys so refund webhooks can revoke exactly the affected
    #: order/subscription without touching unrelated purchases.
    subscription_id: str = ""
    order_id: str = ""

    @classmethod
    def free(cls) -> "License":
        return cls()

    @property
    def is_paid(self) -> bool:
        return self.plan != "free"

    def has(self, feature: str) -> bool:
        return feature in self.features

    def to_public_dict(self) -> dict:
        """JSON-able summary for UIs. Contains no key material."""
        t = dict(trial_status())
        if self.is_trial:                       # a real, server-issued trial key
            t["active"] = True
            if self.expires is not None:
                secs = float(self.expires) - time.time()
                t["days_left"] = max(0, int(secs // 86400) + (1 if secs > 0 else 0))
        return {
            "plan": self.plan, "email": self.email, "seats": self.seats,
            "expires": self.expires, "features": sorted(self.features),
            "key_id": self.key_id, "purchase_url": upgrade_url(),
            "upgrade_url": upgrade_url(), "pro_upgrade_url": upgrade_url("pro"),
            "team_upgrade_url": upgrade_url("team"),
            "is_trial": self.is_trial, "trial": t,
            "known_features": FEATURES,
        }


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


#: Test-only switch, default False in EVERY shipped process. Only the test suite flips
#: it True (from ``tests/conftest.py``) so it can verify against throwaway keypairs.
#: Deliberately NOT keyed off ``"pytest" in sys.modules`` — a dependency that
#: transitively imports pytest at runtime must never be able to re-open the override.
#: Nothing on the production import path (dashboard, CLI, inspector) sets this.
_TEST_MODE_PUBKEY_OVERRIDE = False


def _pubkey_override_allowed() -> bool:
    """Whether the ``ENGRAPHIS_LICENSE_PUBKEY`` override may replace the pinned key.

    True only when the test suite has explicitly opted in via
    :data:`_TEST_MODE_PUBKEY_OVERRIDE`. In a shipped process this is False, which is the
    whole point: the verify key is NOT runtime-configurable."""
    return _TEST_MODE_PUBKEY_OVERRIDE


def vendor_public_key() -> bytes:
    """The pinned vendor Ed25519 verify key — the single trust anchor at runtime.

    SECURITY: this is deliberately NOT overridable in a shipped process. Honoring an
    ``ENGRAPHIS_LICENSE_PUBKEY`` env var here was a full authentication bypass — anyone
    could generate their own keypair, sign a Pro/Team payload with their own private
    seed, point the verifier at their own public key, and pass verification without ever
    touching the vendor's private key. The env override now applies ONLY under the test
    harness (see :func:`_pubkey_override_allowed`). Key rotation is a source change to
    ``_VENDOR_PUBKEY_HEX`` plus a release, never a runtime env var."""
    hexkey = _VENDOR_PUBKEY_HEX
    if _pubkey_override_allowed():
        hexkey = os.environ.get("ENGRAPHIS_LICENSE_PUBKEY", "").strip() or hexkey
    try:
        raw = bytes.fromhex(hexkey)
    except ValueError:
        raise LicenseError("vendor public key is not valid hex")
    if len(raw) != 32:
        raise LicenseError("vendor public key must be 32 bytes")
    return raw


def vendor_public_keys() -> tuple:
    """Current plus temporarily trusted rotation keys, current first."""
    current = vendor_public_key()
    if _pubkey_override_allowed():
        return (current,)
    out = [current]
    for hexkey in _PREVIOUS_VENDOR_PUBKEY_HEXES:
        try:
            raw = bytes.fromhex(hexkey)
        except ValueError:
            raise LicenseError("previous vendor public key is not valid hex")
        if len(raw) != 32:
            raise LicenseError("previous vendor public key must be 32 bytes")
        if raw not in out:
            out.append(raw)
    return tuple(out)


def compose_key(payload: dict, secret: bytes) -> str:
    """Build a signed key from a payload dict (vendor-side; see license_admin)."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = ed25519_sign(secret, body)
    return "%s.%s.%s" % (_KEY_PREFIX, _b64u_encode(body), _b64u_encode(sig))


def parse_key(key: str, *, now: Optional[float] = None) -> License:
    """Verify ``key`` and return its :class:`License`. Raises :class:`LicenseError`."""
    key = (key or "").strip()
    if not key:
        raise LicenseError("empty license key")
    parts = key.split(".")
    if len(parts) != 3 or parts[0] != _KEY_PREFIX:
        raise LicenseError("not an Engraphis license key (expected ENGR1.<payload>.<sig>)")
    try:
        body = _b64u_decode(parts[1])
        sig = _b64u_decode(parts[2])
    except (ValueError, base64.binascii.Error):
        raise LicenseError("license key is not valid base64url")
    verified_with = next(
        (pub for pub in vendor_public_keys() if ed25519_verify(pub, body, sig)), None)
    if verified_with is None:
        raise LicenseError("license signature is invalid (tampered or wrong vendor key)")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LicenseError("license payload is not valid JSON")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise LicenseError("unsupported license payload version")
    verified_key_id = verified_with.hex()[:16]
    signing_key_id = str(payload.get("signing_key_id", "") or "").strip().lower()
    if signing_key_id and signing_key_id != verified_key_id:
        raise LicenseError("license signing-key id does not match its signature")

    plan = str(payload.get("plan", "")).lower()
    if plan not in PLAN_FEATURES:
        raise LicenseError("unknown plan '%s'" % plan)
    expires = payload.get("expires")
    if expires is not None:
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            raise LicenseError("invalid expiry in license")
        if not math.isfinite(expires):
            raise LicenseError("invalid expiry in license")
        current_time = time.time() if now is None else float(now)
        if not math.isfinite(current_time):
            raise LicenseError("invalid license validation time")
        if current_time > expires:
            raise LicenseError("license expired on %s — renew at %s" % (
                time.strftime("%Y-%m-%d", time.gmtime(expires)), upgrade_url()))
    extra = payload.get("features") or []
    if not isinstance(extra, list):
        raise LicenseError("invalid features list in license")
    features = frozenset(PLAN_FEATURES[plan]) | frozenset(str(f) for f in extra)
    try:
        seats = max(1, int(payload.get("seats", 1)))
    except (OverflowError, TypeError, ValueError):
        seats = 1
    return License(
        plan=plan, email=str(payload.get("email", "")), seats=seats,
        issued=payload.get("issued"), expires=expires, features=features,
        key_id=hashlib.sha256(key.encode("ascii")).hexdigest()[:12],
        signing_key_id=verified_key_id,
        is_trial=bool(payload.get("trial")),
        enforce=str(payload.get("enforce", "") or "").strip().lower(),
        cloud_url=str(payload.get("cloud_url", "") or "").strip().rstrip("/"),
        subscription_id=str(payload.get("subscription_id", "") or "").strip()[:128],
        order_id=str(payload.get("order_id", "") or "").strip()[:128],
    )


# ── process-wide current license (cached; free tier on any failure) ───────────────────

_cached: Optional[License] = None
_cache_error: str = ""
_cache_recheck_at: float = float("inf")  # wall-clock time after which the cache is stale
# The cache globals above are read + mutated from FastAPI's threadpool AND invalidated
# by cloud_license on denial, so all access is serialized. Reentrant so current_license
# can be called from a context that already holds it. Without this, a concurrent
# invalidate_cache() between the "is not None" check and the return could return None.
_cache_lock = threading.RLock()
#: Monotonic counter bumped on every cache invalidation or authoritative denial. A
#: current_license() call snapshots it before its (unlocked, network) cloud gate and, when
#: that gate comes back ALLOWED, only stores the paid result if the counter is unchanged.
#: This closes a lost-update race: without it, an older "allowed" computed against
#: pre-revocation state could land in the cache AFTER a newer denial / invalidate_cache()
#: and resurrect a revoked key until the next recheck — defeating immediate fail-closed.
_cache_generation: int = 0

#: Cloud-mode cache lifetime. Each refresh contacts the license server; an authoritative
#: denial fails closed immediately, while a transient network failure may continue using
#: the existing signed lease until its expiry.
_CLOUD_RECHECK_SECONDS = 900


def _license_recheck_at(lic: License, *, now: Optional[float] = None) -> float:
    """Deadline after which :func:`current_license` must re-validate even uncalled with
    ``refresh``. Expiry (key or trial) is a hard deadline — without it, a process that
    outlived its key/trial kept paid features until restart, because the cache was
    immortal. Cloud mode adds the rolling :data:`_CLOUD_RECHECK_SECONDS` bound so
    revocation propagates into long-running processes on lease cadence."""
    now = time.time() if now is None else now
    deadline = float("inf") if lic.expires is None else float(lic.expires)
    # Online-only: every paid license (a purchased key OR a server-issued trial key) is
    # verified against the vendor server, so bound the in-process cache. A revoked key or
    # lapsed lease then propagates within _CLOUD_RECHECK_SECONDS instead of surviving in a
    # long-running process until restart.
    if lic.is_paid:
        deadline = min(deadline, now + _CLOUD_RECHECK_SECONDS)
    return deadline


def _read_key_material() -> str:
    env = os.environ.get("ENGRAPHIS_LICENSE_KEY", "").strip()
    if env:
        return env
    try:
        return _LICENSE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cloud_gate(lic: "License", material: str) -> tuple:
    """Server-authoritative gate — EVERY paid key must present a live vendor lease.

    Online-only by product policy (no offline mode): the verification server is
    ``ENGRAPHIS_CLOUD_URL`` if set, else the URL signed into the key at issuance
    (``lic.cloud_url`` — unforgeable, inside the signed payload), else the built-in
    isolated license service. Unlike the previous opt-in design, a paid key
    is NEVER unlocked by local signature alone: it must register with the server and hold
    an unexpired lease. If no server URL resolves at all (someone blanked the relay URL),
    we DENY — there is deliberately no offline path to paid features. Revoked / expired /
    seat-exceeded keys, and clients offline past their lease window, all fail closed.

    The free tier never reaches here (it has no key), so offline free-tier use is
    unaffected — only Pro/Team features require the server."""
    from engraphis.config import resolve_license_server_url
    base = resolve_license_server_url(lic.cloud_url)
    if not base:
        return False, ("server-side license verification is required for paid features "
                       "but no license server is configured (ENGRAPHIS_CLOUD_URL and the "
                       "license service URL and signed server URL are both empty)")
    try:
        from engraphis import cloud_license
        return cloud_license.gate(lic, material, base_url=base)
    except Exception:  # any error verifying with the server -> fail closed
        return False, ("cloud verification failed; check the license service URL and "
                       "network connection")


def invalidate_cache() -> None:
    """Drop the in-memory license cache so the next :func:`current_license` re-verifies.

    ``cloud_license.revalidate`` calls this the instant the server denies a key (revoked
    /refunded/seat-limit), so a paying customer's revoked entitlement stops working
    immediately instead of lingering until the lease TTL — without forcing the test
    suite to reach into private module state."""
    global _cached, _cache_recheck_at, _cache_generation
    with _cache_lock:
        _cached = None
        _cache_recheck_at = float("inf")
        _cache_generation += 1  # supersede any allow still in flight (fail closed)


def current_license(*, refresh: bool = False) -> License:
    """The server-verified paid/trial license for this process, or ``License.free()``.

    Never raises: a bad, revoked, expired, or currently unverifiable key degrades to the
    free tier and the reason is kept in :func:`license_error`.
    """
    _verify_no_tampering()
    global _cached, _cache_error, _cache_recheck_at, _cache_generation
    # Fast path under the lock: a valid, unexpired cache entry is returned atomically so a
    # concurrent invalidate_cache() can't null it out between the check and the return.
    with _cache_lock:
        if _cached is not None and not refresh and time.time() < _cache_recheck_at:
            return _cached
        # Snapshot the generation for the staleness check below, taken while we still hold
        # the lock so it's consistent with the cache state we're about to (re)compute from.
        gen_at_start = _cache_generation
    # The cloud gate does network I/O, so run it OUTSIDE the lock (two threads racing a
    # cache miss just do redundant, idempotent work — last store wins). Online-only:
    # entitlement comes ONLY from a signature-valid key that ALSO passes the server-side
    # cloud gate. No key, or a server-denied key ⇒ the free tier.
    material = _read_key_material()
    lic: Optional[License] = None
    reason = ""
    if material:
        try:
            lic = parse_key(material)
        except LicenseError as exc:
            lic, reason = None, str(exc)     # bad key → free
        else:
            allowed, gate_reason = _cloud_gate(lic, material)
            if not allowed:
                lic, reason = None, gate_reason  # cloud denied (revoked/unregistered) → free
    with _cache_lock:
        if lic is None:
            # A denial / free fallback is authoritative and must win over any concurrently
            # in-flight allow: bump the generation FIRST so a slower "allowed" result
            # computed against older (pre-revocation) state is discarded by the staleness
            # check below instead of resurrecting a revoked key until the next recheck.
            _cache_generation += 1
            _cache_error = reason
            _cached = License.free()
            # A configured key that temporarily failed its cloud gate must retry
            # automatically. Caching the free fallback forever forced users to restart or
            # paste the same key again after an outage. No-key free installs stay cached.
            _cache_recheck_at = (
                time.time() + _CLOUD_RECHECK_SECONDS
                if material else _license_recheck_at(_cached)
            )
        elif _cache_generation != gen_at_start:
            # An invalidate_cache() or a denial landed while our cloud gate was in flight,
            # so this "allowed" result may already be stale. Fail closed: don't overwrite
            # the current (fail-closed) state — the next refresh re-verifies with the server.
            return _cached if _cached is not None else License.free()
        else:
            _cached, _cache_error = lic, ""
            _cache_recheck_at = _license_recheck_at(lic)
        return _cached


# ── one-time local free trial (grants Pro features, no key, no phone-home) ────────────

def _read_trial() -> dict:
    """Read + HMAC-verify the trial file. Unsigned or tampered files are ignored (return
    ``{}``), so hand-editing ``trial.json`` to extend a trial no longer works."""
    try:
        raw = json.loads(_TRIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    data, sig = raw.get("data"), raw.get("sig")
    if not isinstance(data, dict) or not isinstance(sig, str):
        return {}  # legacy/unsigned or hand-crafted file — not trusted
    if not hmac.compare_digest(sig, _sign_trial(data)):
        return {}  # tampered payload
    return data


def trial_status(*, now: Optional[float] = None) -> dict:
    """Advisory, NON-granting snapshot of trial state for the UI.

    The trial is now a real server-issued key, so entitlement NEVER comes from local
    files. This only reports whether this device already consumed its one-time trial (so
    the UI can stop offering the button). ``used`` is presence-only across the advisory
    stamp plus any legacy markers and grants nothing — deleting it cannot re-arm a trial;
    the server is the authoritative one-trial-per-device gate. ``active``/``days_left``
    for an active trial are filled in by :meth:`License.to_public_dict` from the signed
    key's expiry."""
    used = _trial_used_locally() or _trial_used_elsewhere()
    return {"active": False, "used": bool(used), "days_left": 0,
            "trial_days": TRIAL_DAYS}


def _local_material_license() -> Optional[License]:
    """Parse this device's locally-stored key material (env var or license file), if
    any. Returns ``None`` when there is none, or it fails to parse — expired included,
    since :func:`parse_key` itself raises past ``expires`` — in which case a trial
    should be free to proceed. Used by :func:`start_trial` / :func:`start_team_trial`
    to distinguish "no key", "an active trial" (idempotent no-op), and "a genuinely
    paid key" (refuse) WITHOUT a network round-trip — the key's own signed expiry is
    what bounds a trial (see ``inspector.webhooks._trial_days``), so a purely local
    check is enough to answer "is a trial already running", no cloud gate needed."""
    material = _read_key_material()
    if not material:
        return None
    try:
        return parse_key(material)
    except LicenseError:
        return None


#: Loose RFC-5322-ish check, deliberately permissive — this is a fast local rejection
#: of obvious garbage, not the authoritative check. The relay (``inspector.auth._EMAIL_RE``)
#: is the real gate, since it's the one that actually has to send mail to the address.
_TRIAL_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def start_trial(*, email: str = "", now: Optional[float] = None) -> dict:
    """Begin the one-time self-serve Pro trial by requesting a REAL, short-lived,
    vendor-signed Pro key from the license server — exactly like :func:`start_team_trial`,
    and exactly like a purchase (just Pro-tier, short-lived).

    There is no offline/local trial grant anymore: a trial is a genuine server-issued
    credential, verified by the same server-side gate every paid feature uses, so it
    cannot be forged by editing local files. Since 2026-07-14 the relay also no longer
    hands back a key from this one call — machine_id alone was trivially resettable
    (delete a local file, get another "free" trial forever), so *email* is now required
    and the actual key is minted only once a one-time magic link sent to it is opened
    (see ``inspector.license_cloud``). This function's return value reflects that: on
    the normal successful path there is nothing to activate yet, so it returns
    ``{"pending": True, "message": ...}`` instead of an activated license — the caller
    (the dashboard's "Start trial" button) should surface that message ("check your
    email") rather than assume Pro just turned on.

    If this device already holds an ACTIVE trial key (any plan — the server only ever
    grants one, see ``_local_material_license``), this is an idempotent no-op that
    returns the current status rather than erroring, so a UI that calls this on every
    "Start trial" click doesn't have to special-case "already trialing". Raises
    :class:`LicenseError` if *email* is missing/malformed, if a genuinely PAID key is
    already active, if this device already claimed its one-time trial (server 409), or
    if the license server is unreachable."""
    lic = _local_material_license()
    if lic is not None:
        if lic.is_trial:
            return lic.to_public_dict()
        # A signature-valid, non-trial key exists locally — but _local_material_license()
        # is deliberately signature-only (no cloud round-trip on the common path), so it
        # can't tell a genuinely active paid key from one the cloud gate is denying
        # (revoked, never registered, relay unreachable, seat cap). Refusing the trial on
        # signature validity alone stranded a real user with neither working features
        # (current_license() had already fallen back to the free tier) NOR a way to get a
        # trial — 2026-07-13 incident. Re-check against the actual, cloud-gated
        # entitlement before refusing: only a key CURRENTLY granting something blocks a
        # trial.
        if current_license(refresh=True).is_paid:
            raise LicenseError("a paid license is already active — no trial needed")
    email = (email or "").strip().lower()
    if not email or not _TRIAL_EMAIL_RE.match(email):
        raise LicenseError("a valid email address is required to start a trial")
    from engraphis import cloud_license
    from engraphis.config import resolve_license_server_url
    base = resolve_license_server_url()
    key, reason, pending = cloud_license.request_trial_key(
        base, cloud_license.machine_id(), plan="pro", email=email)
    if pending:
        return {"pending": True, "message": reason}
    if not key:
        raise LicenseError(reason or "could not start the free trial — try again shortly")
    _mark_trial_used()   # advisory-only UI hint; the server is the real one-per-device gate
    return activate(key).to_public_dict()


def start_team_trial(*, email: str = "", now: Optional[float] = None) -> dict:
    """Begin the one-time self-serve Team trial by requesting a real, short-lived,
    vendor-signed Team key from the license server. Pro and Team trials both use the
    same server-authoritative issuance and verification path as purchased keys.

    The network round-trip is required, not incidental: the resulting key is later
    presented to other server-side gates (the team-invite relay and ``/register``)
    that only accept a genuinely vendor-signed credential. Without it, a trialing user
    could open Team mode locally but could never send an invite.

    Since 2026-07-14 *email* is required and the key is minted only once a one-time
    magic link sent to it is opened — see :func:`start_trial`'s docstring for the full
    reasoning (same relay endpoint, same hardening). The normal successful return here
    is likewise ``{"pending": True, "message": ...}``, not an activated license.

    If this device already holds an ACTIVE trial key (any plan), this is an
    idempotent no-op that returns the current status — same reasoning as
    :func:`start_trial`. Raises :class:`LicenseError` if *email* is missing/malformed,
    if a genuinely PAID key is already active, if this device's one-time trial is
    already spent (relay 409), or if the relay is unreachable. ``now`` is accepted
    (unused) only for signature parity with :func:`start_trial`."""
    lic = _local_material_license()
    if lic is not None:
        if lic.is_trial:
            return lic.to_public_dict()
        # See the matching comment in start_trial() above — same 2026-07-13 incident,
        # same fix: only refuse when the key is CURRENTLY entitling something.
        if current_license(refresh=True).is_paid:
            raise LicenseError("a paid license is already active — no trial needed")
    email = (email or "").strip().lower()
    if not email or not _TRIAL_EMAIL_RE.match(email):
        raise LicenseError("a valid email address is required to start a trial")
    from engraphis import cloud_license
    from engraphis.config import resolve_license_server_url
    base = resolve_license_server_url()
    key, reason, pending = cloud_license.request_team_trial_key(
        base, cloud_license.machine_id(), email=email)
    if pending:
        return {"pending": True, "message": reason}
    if not key:
        raise LicenseError(reason or "could not start the Team trial — try again shortly")
    _mark_trial_used()   # advisory-only UI hint; the server is the real one-per-device gate
    return activate(key).to_public_dict()


def license_error() -> str:
    """Why the configured key (if any) was rejected — '' when none/valid."""
    current_license()
    return _cache_error


def activate(key: str) -> License:
    """Verify ``key``, persist it to ``~/.engraphis/license.key``, refresh the cache."""
    parse_key(key)  # raises LicenseError if bad — nothing persisted then
    _LICENSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Create a private sibling from the first byte, fsync it, then atomically replace the
    # destination. A failed reactivation therefore leaves the last valid key intact and
    # never writes through an existing permissive inode or symlink.
    fd, temp_name = tempfile.mkstemp(
        prefix=".license.key.", dir=str(_LICENSE_FILE.parent))
    temp_path = Path(temp_name)
    try:
        try:
            os.chmod(temp_path, 0o600)
        except OSError:  # e.g. some Windows filesystems
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(key.strip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(_LICENSE_FILE))
        try:
            os.chmod(_LICENSE_FILE, 0o600)
        except OSError:  # e.g. some Windows filesystems
            pass
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return current_license(refresh=True)


def has_feature(feature: str) -> bool:
    _verify_no_tampering()
    return current_license().has(feature)


def require_cloud_lease(feature: str) -> None:
    """Require a live, server-verified cloud lease for a paid feature.

    Unlike :func:`has_feature` / :func:`require_feature` (which can be patched
    in a forked client), this gate forces a **fresh** server round-trip via
    ``current_license(refresh=True)``. The server must return a valid Ed25519-
    signed lease — which a patched client cannot forge without the vendor's
    private key. This ties local-only features (analytics, export, automation)
    to the same server-side enforcement that protects sync and team.

    Raises :class:`LicenseError` if no active license or the cloud lease is
    absent/denied/expired.
    """
    _verify_no_tampering()
    lic = current_license(refresh=True)
    if not lic.has(feature):
        raise LicenseError(
            "'%s' is an Engraphis %s feature (%s). Start a %d-day free trial from the "
            "dashboard's Settings → License panel, or buy at %s and paste the key."
            % (feature, required_plan(feature).capitalize(),
               FEATURES.get(feature, feature), TRIAL_DAYS, upgrade_url(required_plan(feature))),
            feature=feature)
    # The cloud gate was already checked in current_license(refresh=True) above.
    # No separate lease check needed — if the gate denied it, lic would be free().


def required_plan(feature: str) -> str:
    """The cheapest plan that unlocks ``feature`` ('team' for anything unknown)."""
    for plan in ("pro", "team"):
        if feature in PLAN_FEATURES[plan]:
            return plan
    return "team"


def require_feature(feature: str) -> None:
    """Raise :class:`LicenseError` with an actionable message if ``feature`` is locked.

    This is THE gate helper — every paid surface (Inspector routes, report scripts)
    funnels through here, so upgrade messaging changes in exactly one place."""
    _verify_no_tampering()
    if not has_feature(feature):
        desc = FEATURES.get(feature, feature)
        tier = required_plan(feature)
        raise LicenseError(
            "'%s' is an Engraphis %s feature (%s). Start a %d-day free trial from the "
            "dashboard's Settings → License panel (email confirmation required), or buy "
            "at %s and "
            "paste the key there, set ENGRAPHIS_LICENSE_KEY, or save it to "
            "~/.engraphis/license.key."
            % (feature, tier.capitalize(), desc, TRIAL_DAYS, upgrade_url(tier)),
            feature=feature)


# ── ship-safety guards (advisory; never raise, never touch the free tier) ─────────

def is_default_vendor_key() -> bool:
    """True while the active vendor key is still the known-compromised DEV keypair.

    The dev private seed has been on dev boxes / in agent sessions and must never be
    active for selling. (The private key never ships in this repo — ``.secrets/`` is
    gitignored — but off-repo exposure of the seed is the real forging risk.) Rotate with
    ``python -m scripts.license_admin keygen`` and pin the printed public key in
    ``_VENDOR_PUBKEY_HEX`` to flip this False. (The env override no longer changes the
    verify key in a shipped process — see :func:`vendor_public_key`.)"""
    try:
        return vendor_public_key() == bytes.fromhex(_DEV_VENDOR_PUBKEY_HEX)
    except LicenseError:
        return False


def production_warnings() -> list:
    """Config that's safe for local use but unsafe for *selling* licenses.

    Advisory only — returns human-readable strings, never raises, and has no effect on
    the free tier or on verification. Entry points (Inspector, license CLI) print these
    at startup so an operator can't accidentally ship the dev signing key or bill against
    a placeholder checkout link."""
    warns = []
    if not VENDOR_SIGNER_RELEASE_READY:
        warns.append(
            "vendor signer is still marked pre-sale. Audit issued keys, rotate the seed "
            "on a trusted machine, update the pinned public key, and set "
            "VENDOR_SIGNER_RELEASE_READY=True in the reviewed rotation release.")
    if is_default_vendor_key():
        warns.append(
            "vendor signing key is the built-in DEV keypair, whose private half has been "
            "on dev boxes / in agent sessions and is treated as compromised. Anyone holding "
            "that seed can forge Pro/Team keys. Generate a replacement into a new secure "
            "path, then complete the reviewed compatibility/reissue ceremony in "
            "docs/COMMERCIAL_OPERATIONS.md.")
    if "github.com" in upgrade_url():
        warns.append(
            "upgrade link still points at the GitHub pricing anchor, not a real checkout. "
            "Set ENGRAPHIS_UPGRADE_URL to your checkout page URL before charging.")
    if _paid_available() and not VENDOR_SIGNER_RELEASE_READY:
        warns.append(
            "ENGRAPHIS_PAID_AVAILABLE is set but VENDOR_SIGNER_RELEASE_READY is still "
            "False — customers could be charged via the live checkout before licenses "
            "can be issued. Complete the signer rotation ceremony before enabling paid "
            "sales.")
    return warns


# ── compilation integrity guard — runs at import time ────────────────────────────

def _verify_module_integrity():
    """Detect if this module was replaced with editable source after a compiled
    extension was already installed.

    If a compiled native extension (.pyd/.so) exists alongside this file, then
    Python's default import order should have loaded that instead — the fact that
    we're running as .py means someone removed or renamed the extension, likely
    to patch the source. If no compiled extension exists (pure-python install on
    a platform without wheels), the check passes.

    There is deliberately NO env-var escape hatch (Phase 2 hardening). Dev
    installs (pip install -e .) never build .pyd/.so, so the check passes
    naturally without any env-var bypass.
    """
    mod = sys.modules.get(__name__)
    if mod is None:
        return
    f = getattr(mod, "__file__", "")
    if not f:
        return
    if not f.endswith(".py"):
        return
    from importlib.machinery import EXTENSION_SUFFIXES
    dirname = os.path.dirname(f)
    basename = os.path.splitext(os.path.basename(f))[0]
    for suffix in EXTENSION_SUFFIXES:
        if os.path.exists(os.path.join(dirname, basename + suffix)):
            raise LicenseError(
                "Engraphis licensing integrity check failed: a compiled native "
                "extension (.pyd/.so) exists but is not being loaded — the module "
                "may have been replaced with editable source. Reinstall Engraphis "
                "from the official distribution (pip install --force-reinstall "
                "engraphis)."
            )


_lock_sentinel = object()
_LOCK_SNAPSHOT = None
_LOCK_TAKEN = False


def _verify_no_tampering():
    """Verify critical gate callables haven't been monkeypatched since import.

    The snapshot is taken lazily on the first gate call, so test-mode setup
    (conftest sets ``_TEST_MODE_PUBKEY_OVERRIDE=True`` after import) is
    captured correctly without false positives.
    """
    global _LOCK_SNAPSHOT, _LOCK_TAKEN
    if not _LOCK_TAKEN:
        _LOCK_SNAPSHOT = {
            "has_feature": has_feature,
            "require_feature": require_feature,
            "current_license": current_license,
            "parse_key": parse_key,
            "ed25519_verify": ed25519_verify,
            "vendor_public_key": vendor_public_key,
            "vendor_public_keys": vendor_public_keys,
        }
        _LOCK_TAKEN = True
    snap = _LOCK_SNAPSHOT
    if snap is None or not isinstance(snap, dict):
        raise LicenseError(
            "Licensing integrity violation: tamper-detection snapshot has been "
            "corrupted. Reinstall Engraphis from the official distribution."
        )
    if _TEST_MODE_PUBKEY_OVERRIDE:
        return  # test mode: skip callable checks
    g = globals()
    for name, expected in snap.items():
        current = g.get(name, _lock_sentinel)
        if current is _lock_sentinel or current != expected:
            raise LicenseError(
                "Licensing integrity violation: '%s' has been tampered with at "
                "runtime. Reinstall Engraphis from the official distribution." % name
            )


def _snapshot_critical_globals():
    """Force a re-snapshot of critical globals (used by tests after mocking)."""
    global _LOCK_SNAPSHOT, _LOCK_TAKEN
    _LOCK_SNAPSHOT = {
        "_TEST_MODE_PUBKEY_OVERRIDE": _TEST_MODE_PUBKEY_OVERRIDE,
        "has_feature": has_feature,
        "require_feature": require_feature,
        "current_license": current_license,
        "parse_key": parse_key,
        "ed25519_verify": ed25519_verify,
        "vendor_public_key": vendor_public_key,
        "vendor_public_keys": vendor_public_keys,
    }
    _LOCK_TAKEN = True


_verify_module_integrity()
