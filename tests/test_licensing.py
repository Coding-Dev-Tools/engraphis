"""Licensing tests — RFC 8032 vectors, issue/verify roundtrip, gates. Runs on the
numpy-only CI gate (pure stdlib, like the module under test)."""
import time

import pytest

from engraphis import licensing as lic
from engraphis.licensing import (
    License, LicenseError, compose_key, current_license, ed25519_public_key,
    ed25519_sign, ed25519_verify, has_feature, parse_key, require_feature,
)

# ── RFC 8032 §7.1 test vectors (TEST 1–3) — the crypto must match the spec exactly ────
_VECTORS = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bac"
     "c61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e"
     "458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290"
     "ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
]


@pytest.mark.parametrize("sk,pk,msg,sig", _VECTORS)
def test_rfc8032_vectors(sk, pk, msg, sig):
    sk, pk = bytes.fromhex(sk), bytes.fromhex(pk)
    msg, sig = bytes.fromhex(msg), bytes.fromhex(sig)
    assert ed25519_public_key(sk) == pk
    assert ed25519_sign(sk, msg) == sig
    assert ed25519_verify(pk, msg, sig)
    assert not ed25519_verify(pk, msg + b"!", sig)          # message tamper
    bad_sig = bytes([sig[0] ^ 1]) + sig[1:]
    assert not ed25519_verify(pk, msg, bad_sig)             # signature tamper


def test_verify_rejects_malformed_inputs_without_raising():
    assert not ed25519_verify(b"short", b"m", b"s" * 64)
    assert not ed25519_verify(b"\xff" * 32, b"m", b"\xff" * 64)  # non-canonical junk


# ── license keys ───────────────────────────────────────────────────────────────────────

SECRET = bytes(range(32))  # deterministic test vendor keypair


@pytest.fixture(autouse=True)
def _test_vendor_key(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    lic.current_license(refresh=True)
    yield
    lic.current_license(refresh=True)


def _issue(plan="pro", days=365, **kw):
    payload = {"v": 1, "plan": plan, "email": "t@x.co", "seats": kw.pop("seats", 1),
               "issued": int(time.time()),
               "expires": int(time.time() + days * 86400) if days else None}
    payload.update(kw)
    return compose_key(payload, SECRET)


def test_roundtrip_pro_key():
    parsed = parse_key(_issue("pro"))
    assert parsed.plan == "pro" and parsed.is_paid
    assert parsed.has("analytics") and parsed.has("export") and not parsed.has("team")
    assert parsed.key_id and "ENGR1" not in parsed.to_public_dict().values()


def test_team_plan_includes_pro_features_and_seats():
    parsed = parse_key(_issue("team", seats=7))
    assert parsed.features >= {"analytics", "export", "team"}
    assert parsed.seats == 7


def test_tampered_payload_rejected():
    key = _issue("pro")
    head, body, sig = key.split(".")
    swapped = body[:-2] + ("AA" if body[-2:] != "AA" else "BB")
    with pytest.raises(LicenseError, match="signature"):
        parse_key(".".join([head, swapped, sig]))


def test_expired_key_rejected_with_renewal_hint():
    key = _issue("pro", days=1)
    with pytest.raises(LicenseError, match="expired"):
        parse_key(key, now=time.time() + 2 * 86400)


def test_perpetual_key_never_expires():
    key = _issue("pro", days=0)
    assert parse_key(key, now=time.time() + 3650 * 86400).plan == "pro"


@pytest.mark.parametrize("bad", ["", "garbage", "ENGR1.only-two", "ENGR2.x.y",
                                 "ENGR1.!!!.???"])
def test_malformed_keys_rejected(bad):
    with pytest.raises(LicenseError):
        parse_key(bad)


def test_unknown_plan_rejected():
    with pytest.raises(LicenseError, match="plan"):
        parse_key(compose_key({"v": 1, "plan": "galactic"}, SECRET))


def test_wrong_vendor_key_rejected(monkeypatch):
    key = _issue("pro")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY",
                       ed25519_public_key(b"\x07" * 32).hex())
    with pytest.raises(LicenseError, match="signature"):
        parse_key(key)


# ── process-level gates ────────────────────────────────────────────────────────────────

def test_free_tier_is_default_not_error():
    assert current_license(refresh=True) == License.free()
    assert not has_feature("analytics")
    assert lic.license_error() == ""


def test_env_key_activates_features(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _issue("team"))
    assert current_license(refresh=True).plan == "team"
    assert has_feature("team")
    require_feature("analytics")  # must not raise


def test_bad_env_key_degrades_to_free_with_reason(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", "ENGR1.!!!.???")
    assert current_license(refresh=True) == License.free()
    assert lic.license_error() != ""


def test_require_feature_message_is_actionable():
    with pytest.raises(LicenseError, match="polar.sh"):
        require_feature("analytics")


def test_require_feature_carries_feature_for_structured_402():
    with pytest.raises(LicenseError) as ei:
        require_feature("analytics")
    assert ei.value.feature == "analytics"
    # non-gate errors carry no feature — HTTP layers can tell the two apart
    with pytest.raises(LicenseError) as ei2:
        parse_key("garbage")
    assert ei2.value.feature is None


def test_require_feature_names_the_right_tier():
    with pytest.raises(LicenseError, match="Pro feature"):
        require_feature("analytics")
    with pytest.raises(LicenseError, match="Team feature"):
        require_feature("team")


def test_required_plan_maps_features_to_cheapest_tier():
    assert lic.required_plan("analytics") == "pro"
    assert lic.required_plan("export") == "pro"
    assert lic.required_plan("team") == "team"
    assert lic.required_plan("unknown-flag") == "team"


def test_upgrade_url_default_is_the_polar_checkout(monkeypatch):
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    assert lic.upgrade_url() == lic.DEFAULT_UPGRADE_URL
    assert "polar.sh" in lic.upgrade_url()


def test_upgrade_url_env_override_wins_everywhere(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://example.com/buy")
    assert lic.upgrade_url() == "https://example.com/buy"
    assert License.free().to_public_dict()["upgrade_url"] == "https://example.com/buy"
    with pytest.raises(LicenseError, match="example.com/buy"):
        require_feature("analytics")


# ── ship-safety guards ────────────────────────────────────────────────

def test_default_vendor_key_is_detected_and_warned(monkeypatch):
    # The dev sentinel must be detected and warned about wherever it is active
    # (env override OR the pinned constant — the machinery is the same either way).
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", lic._DEV_VENDOR_PUBKEY_HEX)
    assert lic.is_default_vendor_key() is True
    warns = lic.production_warnings()
    assert any("DEV key" in w for w in warns)


def test_shipped_pinned_key_is_not_the_compromised_dev_key():
    # Rotation guard: the verify key pinned in the repo must NOT equal the old dev
    # sentinel. If this fails, someone re-pinned the known-compromised dev key — rotate
    # again (`python -m scripts.license_admin keygen --force`).
    assert lic._VENDOR_PUBKEY_HEX != lic._DEV_VENDOR_PUBKEY_HEX


def test_rotated_vendor_key_clears_the_dev_key_warning(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(b"\x09" * 32).hex())
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://buy.example.com/engraphis")
    assert lic.is_default_vendor_key() is False
    assert lic.production_warnings() == []


def test_production_warnings_flag_placeholder_checkout(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(b"\x09" * 32).hex())
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://github.com/Coding-Dev-Tools/engraphis")
    assert any("checkout" in w for w in lic.production_warnings())
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://buy.example.com/engraphis")
    assert not any("checkout" in w for w in lic.production_warnings())


def test_production_warnings_never_raise_and_return_strings():
    for w in lic.production_warnings():
        assert isinstance(w, str) and w


def test_activate_persists_key(monkeypatch, tmp_path):
    target = tmp_path / "license.key"
    monkeypatch.setattr(lic, "_LICENSE_FILE", target)
    key = _issue("pro")
    out = lic.activate(key)
    assert out.plan == "pro"
    assert target.read_text().strip() == key
    with pytest.raises(LicenseError):
        lic.activate("ENGR1.bad.key")            # invalid key: not persisted…
    assert target.read_text().strip() == key     # …previous key untouched
