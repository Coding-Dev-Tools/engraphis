"""Offline license verification for the Engraphis paid tiers (commercial/open-core layer).

Open-core with signed keys: the free core is complete and Apache-2.0; Pro/Team features
activate with an ``ENGR1.<payload>.<signature>`` key whose JSON payload is Ed25519-signed
by the vendor. Verification is **offline and pure stdlib** — no phone-home, no license
server, no new dependency — so the numpy-only core guarantee (AGENTS.md §3) and the
local-first promise both hold. A determined user can fork the gate out (Apache-2.0); that
is the accepted Sidekiq-style trade — we sell convenience and support, not DRM.

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
import json
import os
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
    "analytics": "Analytics dashboard — growth, retention distribution, decay forecast",
    "export": "Compliance export — full bi-temporal workspace dump (memories + audit)",
    "team": "Team mode — multi-user Inspector with logins and roles",
}

#: What each plan unlocks. Unknown feature names in a key are carried but inert.
PLAN_FEATURES: dict = {
    "pro": frozenset({"analytics", "export"}),
    "team": frozenset({"analytics", "export", "team"}),
}

#: Where to buy — shown by the Inspector's license dialog and error messages.
#: ``ENGRAPHIS_UPGRADE_URL`` overrides (set it to the live checkout/pricing page);
#: the default is the Polar checkout link.
DEFAULT_UPGRADE_URL = "https://buy.polar.sh/polar_cl_n6CR3ERqOus2VUhRrGrsRUqOB8yjDTeEU7p1r3CRrae"


def upgrade_url() -> str:
    """The URL a free user should visit to upgrade. Env-configurable, never empty."""
    return os.environ.get("ENGRAPHIS_UPGRADE_URL", "").strip() or DEFAULT_UPGRADE_URL

_KEY_PREFIX = "ENGR1"
# Pinned **production** Ed25519 verify key (32-byte public half). Rotated 2026-07-08
# away from the dev keypair (see _DEV_VENDOR_PUBKEY_HEX below): the dev private seed had
# been on dev boxes / in agent sessions, so it is treated as compromised and was
# overwritten. The new private seed lives ONLY in the gitignored `.secrets/vendor_signing.key`
# on the issuance machine — it never ships in this repo, never in .env, never in any agent
# session. Anyone with only this repo CANNOT forge a valid key. Re-generate on an
# offline/trusted machine before the first real sale.
# ROTATE BEFORE SELLING: run `python -m scripts.license_admin keygen --force` and replace
# this constant with the printed public key.
_VENDOR_PUBKEY_HEX = "d3520482d87a22f7e39e95cfa4b40bc2460f2576213868ba99ea1e4ea7719e08"
# Frozen fingerprint of the OLD, known-compromised dev keypair. Kept as a sentinel so
# is_default_vendor_key() / production_warnings() can flag it if anyone ever re-pins it.
# Its private half does NOT ship in this repo (`.secrets/` is gitignored), but it was
# exposed in dev boxes / agent sessions and must never be the active key for selling.
_DEV_VENDOR_PUBKEY_HEX = "4722dc145d7b988f6a2513e750e367beb2dd75a68a208c8546b1fbb61c862b7e"

_LICENSE_FILE = Path.home() / ".engraphis" / "license.key"


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
        return {
            "plan": self.plan, "email": self.email, "seats": self.seats,
            "expires": self.expires, "features": sorted(self.features),
            "key_id": self.key_id, "purchase_url": upgrade_url(),
            "upgrade_url": upgrade_url(),
            "known_features": FEATURES,
        }


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def vendor_public_key() -> bytes:
    """Pinned vendor key; ``ENGRAPHIS_LICENSE_PUBKEY`` (hex) overrides for rotation/tests."""
    hexkey = os.environ.get("ENGRAPHIS_LICENSE_PUBKEY", "").strip() or _VENDOR_PUBKEY_HEX
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
    )


# ── process-wide current license (cached; free tier on any failure) ───────────────────

_cached: Optional[License] = None
_cache_error: str = ""


def _read_key_material() -> str:
    env = os.environ.get("ENGRAPHIS_LICENSE_KEY", "").strip()
    if env:
        return env
    try:
        return _LICENSE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def current_license(*, refresh: bool = False) -> License:
    """The verified license for this process, or ``License.free()``. Never raises —
    a bad key degrades to the free tier and the reason is kept in :func:`license_error`."""
    global _cached, _cache_error
    if _cached is not None and not refresh:
        return _cached
    material = _read_key_material()
    if not material:
        _cached, _cache_error = License.free(), ""
        return _cached
    try:
        _cached, _cache_error = parse_key(material), ""
    except LicenseError as exc:
        _cached, _cache_error = License.free(), str(exc)
    return _cached


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
            "'%s' is an Engraphis %s feature (%s). Start a 3-day free trial at %s, then paste your key "
            "in the Inspector's license dialog, set ENGRAPHIS_LICENSE_KEY, or save it "
            "to ~/.engraphis/license.key."
            % (feature, tier.capitalize(), desc, upgrade_url()), feature=feature)


# ── ship-safety guards (advisory; never raise, never touch the free tier) ─────────

def is_default_vendor_key() -> bool:
    """True while the active vendor key is still the known-compromised DEV keypair.

    The dev private seed has been on dev boxes / in agent sessions and must never be
    active for selling. (The private key never ships in this repo — ``.secrets/`` is
    gitignored — but off-repo exposure of the seed is the real forging risk.) Rotate with
    ``python -m scripts.license_admin keygen`` and pin the printed public key in
    ``_VENDOR_PUBKEY_HEX`` (or set ``ENGRAPHIS_LICENSE_PUBKEY``) to flip this False."""
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
