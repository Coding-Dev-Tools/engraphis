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
import os
import re
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


def upgrade_url(plan: Optional[str] = None) -> str:
    """The URL a user should visit to buy ``plan`` (defaults to the Pro/general link).

    Env-configurable and never empty: ``ENGRAPHIS_TEAM_UPGRADE_URL`` for Team,
    ``ENGRAPHIS_PRO_UPGRADE_URL`` (or the legacy ``ENGRAPHIS_UPGRADE_URL``) for Pro."""
    if (plan or "").lower() == "team":
        return (os.environ.get("ENGRAPHIS_TEAM_UPGRADE_URL", "").strip()
                or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip()
                or DEFAULT_TEAM_UPGRADE_URL)
    return (os.environ.get("ENGRAPHIS_PRO_UPGRADE_URL", "").strip()
            or os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip()
            or DEFAULT_PRO_UPGRADE_URL)

_KEY_PREFIX = "ENGR1"
# Pinned **production** Ed25519 verify key (32-byte public half). Rotated 2026-07-11
# to a fresh CSPRNG keypair, retiring BOTH the original dev keypair (see
# _DEV_VENDOR_PUBKEY_HEX below) and the interim 2026-07-08 key d3520482…9e08, which is
# now treated as retired out of caution. Any license signed by an older seed no longer
# verifies. The private seed lives ONLY in the gitignored `.secrets/vendor_signing.key`
# on the issuance machine — it never ships in this repo, never in .env, never in any agent
# session. Anyone with only this repo CANNOT forge a valid key. Re-generate on an
# offline/trusted machine before the first real sale.
# ROTATE BEFORE SELLING: run `python -m scripts.license_admin keygen --force` and replace
# this constant with the printed public key.
_VENDOR_PUBKEY_HEX = "0f9ede880d65184f4615221d03e8127c38e1b7a8f8d789a050780ae50c36421d"
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


def _write_trial_tombstones(payload: dict) -> None:
    """Best-effort: record trial consumption in every independent location. Content is
    informational (signed for forensics); presence is what :func:`_trial_used_elsewhere`
    checks. A location that can't be written is skipped — the others still hold."""
    body = json.dumps({"data": payload, "sig": _sign_trial(payload)})
    for path in _trial_tombstone_files():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
        except OSError:
            continue

#: Monotonic wall-clock anchor. Persists the highest wall-clock time ever observed so a
#: user cannot roll the system clock backward to resurrect an expired key/lease or stretch
#: a trial. Advisory (the file is local and deletable) — it just closes the trivial
#: "set the date back" bypass; real expiry enforcement is the cloud lease.
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
    is_trial: bool = False  # True when this License is a time-boxed local trial grant
    #: ``"cloud"`` = this key is ONLY valid with a live server-side lease (see
    #: :func:`_cloud_gate`); "" = classic offline verification. Part of the signed
    #: payload, so a customer cannot strip it without breaking the signature.
    enforce: str = ""
    #: License-server URL baked into the key at issuance — also signed/unforgeable.
    cloud_url: str = ""
    #: Optional vendor-side identifiers used only for server revocation lookups. They are
    #: signed into auto-issued keys so refund webhooks can revoke exactly the affected key.
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
    if not ed25519_verify(vendor_public_key(), body, sig):
        raise LicenseError("license signature is invalid (tampered or wrong vendor key)")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise LicenseError("license payload is not valid JSON")
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise LicenseError("unsupported license payload version")

    plan = str(payload.get("plan", "")).lower()
    if plan not in PLAN_FEATURES:
        raise LicenseError("unknown plan '%s'" % plan)
    expires = payload.get("expires")
    if expires is not None:
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            raise LicenseError("invalid expiry in license")
        if (now if now is not None else time.time()) > expires:
            raise LicenseError("license expired on %s — renew at %s" % (
                time.strftime("%Y-%m-%d", time.gmtime(expires)), upgrade_url()))
    extra = payload.get("features") or []
    if not isinstance(extra, list):
        raise LicenseError("invalid features list in license")
    features = frozenset(PLAN_FEATURES[plan]) | frozenset(str(f) for f in extra)
    try:
        seats = max(1, int(payload.get("seats", 1)))
    except (TypeError, ValueError):
        seats = 1
    return License(
        plan=plan, email=str(payload.get("email", "")), seats=seats,
        issued=payload.get("issued"), expires=expires, features=features,
        key_id=hashlib.sha256(key.encode("ascii")).hexdigest()[:12],
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

#: Cloud-mode cache lifetime. Bounded so a REVOKED key degrades in a long-running
#: process within minutes of its lease lapsing instead of at the next restart. The
#: re-check is cheap: the stored lease verifies locally, so no network round-trip
#: happens until the lease itself needs renewing.
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
    vendor relay (``settings.relay_url``). Unlike the previous opt-in design, a paid key
    is NEVER unlocked by local signature alone: it must register with the server and hold
    an unexpired lease. If no server URL resolves at all (someone blanked the relay URL),
    we DENY — there is deliberately no offline path to paid features. Revoked / expired /
    seat-exceeded keys, and clients offline past their lease window, all fail closed.

    The free tier never reaches here (it has no key), so offline free-tier use is
    unaffected — only Pro/Team features require the server."""
    from engraphis.config import settings
    base = (os.environ.get("ENGRAPHIS_CLOUD_URL", "").strip()
            or lic.cloud_url
            or (settings.relay_url or "").strip())
    if not base:
        return False, ("server-side license verification is required for paid features "
                       "but no license server is configured (ENGRAPHIS_CLOUD_URL and the "
                       "vendor relay URL are both empty)")
    try:
        from engraphis import cloud_license
        return cloud_license.gate(lic, material, base_url=base)
    except Exception as exc:  # any error verifying with the server → fail closed
        return False, "cloud verification error: %s" % exc


def current_license(*, refresh: bool = False) -> License:
    """The verified license for this process, or a trial/``License.free()``. Never
    raises — a bad key degrades (to an active trial, else the free tier) and the reason
    is kept in :func:`license_error`. A valid paid key always takes precedence over a
    trial; an active local trial takes precedence over free."""
    global _cached, _cache_error, _cache_recheck_at
    if _cached is not None and not refresh and time.time() < _cache_recheck_at:
        return _cached
    material = _read_key_material()
    if material:
        try:
            lic = parse_key(material)
        except LicenseError as exc:
            _cache_error = str(exc)  # bad key → fall through to trial/free
        else:
            allowed, reason = _cloud_gate(lic, material)
            if allowed:
                _cached, _cache_error = lic, ""
                _cache_recheck_at = _license_recheck_at(lic)
                return _cached
            _cache_error = reason    # cloud denied (revoked/unregistered) → trial/free
    else:
        _cache_error = ""
    # Online-only: entitlement comes ONLY from a signature-valid key that ALSO passes the
    # server-side cloud gate above. There is no local/offline trial grant anymore — the
    # free trial is a real, short-lived, server-issued key (see start_trial) that flows
    # through the exact same gate. No key, or a server-denied key ⇒ the free tier.
    _cached = License.free()
    _cache_recheck_at = _license_recheck_at(_cached)
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


def _trial_license(status: dict) -> License:
    """RETIRED — no longer called. The offline/local trial grant it used to synthesize was
    a bypass (any local file could mint Pro without the server). Trials are now real
    server-issued keys. Kept only so stray references don't break; do NOT reintroduce it
    into :func:`current_license`."""
    return License(plan="pro", email="trial", seats=1,
                   issued=status.get("started"), expires=status.get("expires"),
                   features=frozenset(PLAN_FEATURES["pro"]), key_id="trial",
                   is_trial=True)


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
    from engraphis.config import settings
    base = os.environ.get("ENGRAPHIS_CLOUD_URL", "").strip() or settings.relay_url
    key, reason, pending = cloud_license.request_trial_key(
        base, cloud_license.machine_id(), plan="pro", email=email)
    if pending:
        return {"pending": True, "message": reason}
    if not key:
        raise LicenseError(reason or "could not start the free trial — try again shortly")
    _mark_trial_used()   # advisory-only UI hint; the server is the real one-per-device gate
    return activate(key).to_public_dict()


def start_team_trial(*, email: str = "", now: Optional[float] = None) -> dict:
    """Begin the one-time self-serve Team trial: unlike :func:`start_trial` (Pro,
    fully local/offline), this requests a REAL signed ``team`` key from the vendor
    relay exactly like a purchased key would be. The extra network round-trip is
    required, not incidental: the resulting key is later presented to OTHER
    server-side gates (the team-invite relay, ``/register``) that only accept a
    genuinely vendor-signed credential, and an offline client-only claim (like the Pro
    trial's used to be) can never satisfy those — see ``inspector.license_cloud.
    start_team_trial`` for the full reasoning. Without this, a trialing user could
    open Team mode locally but could never actually send a team invite, which defeats
    the point of letting them trial Team at all.

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
    from engraphis.config import settings
    base = os.environ.get("ENGRAPHIS_CLOUD_URL", "").strip() or settings.relay_url
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
    _LICENSE_FILE.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(_LICENSE_FILE, 0o600)
    except OSError:  # e.g. some Windows filesystems
        pass
    return current_license(refresh=True)


def has_feature(feature: str) -> bool:
    return current_license().has(feature)


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
    if not has_feature(feature):
        desc = FEATURES.get(feature, feature)
        tier = required_plan(feature)
        raise LicenseError(
            "'%s' is an Engraphis %s feature (%s). Start a %d-day free trial from the "
            "dashboard's Settings → License panel (one click, no key), or buy at %s and "
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
    if is_default_vendor_key():
        warns.append(
            "vendor signing key is the built-in DEV keypair, whose private half has been "
            "on dev boxes / in agent sessions and is treated as compromised. Anyone holding "
            "that seed can forge Pro/Team keys. Rotate before selling: run "
            "`python -m scripts.license_admin keygen --force`, then pin the printed public "
            "key in engraphis/licensing.py (_VENDOR_PUBKEY_HEX).")
    if "github.com" in upgrade_url():
        warns.append(
            "upgrade link still points at the GitHub pricing anchor, not a real checkout. "
            "Set ENGRAPHIS_UPGRADE_URL to your checkout page URL before charging.")
    return warns
