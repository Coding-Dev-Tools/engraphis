"""Licensing tests — RFC 8032 vectors, issue/verify roundtrip, gates. Runs on the
numpy-only CI gate (pure stdlib, like the module under test)."""
import json
import sqlite3
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
    lic._cached = None
    yield
    # Reset the cache directly rather than re-running the gate at teardown — under
    # online-only that would try to reach the vendor server after the lease-granting
    # fixture has already been torn down.
    lic._cached = None
    lic._cache_error = ""
    lic._cache_recheck_at = float("inf")


@pytest.fixture(autouse=True)
def _grant_cloud_lease(monkeypatch, tmp_path):
    """Online-only enforcement requires a live vendor lease for every paid key. These unit
    tests exercise the LOCAL signature/gate logic, so stub the server to APPROVE (return a
    lease signed with the test vendor seed) and reroute client lease/device state to tmp.
    Denial / fail-closed paths live in tests/test_online_only_enforcement.py."""
    from engraphis import cloud_license as _cl
    monkeypatch.setattr(_cl, "_DIR", tmp_path)
    monkeypatch.setattr(_cl, "_LEASE_FILE", tmp_path / "lease.sig")
    monkeypatch.setattr(_cl, "_MACHINE_ID_FILE", tmp_path / "machine_id")
    _cl._machine_id_cache.clear()

    def _register(base, key, mid, **kw):
        try:
            parsed = parse_key(key)
        except Exception:
            return None
        now = int(time.time())
        return _cl.compose_lease(
            {"v": 1, "key_id": parsed.key_id, "plan": parsed.plan,
             "features": sorted(parsed.features), "machine_id": mid,
             "issued": now, "expires": now + 3600}, SECRET)
    monkeypatch.setattr(_cl, "register", _register)


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


@pytest.mark.parametrize("invalid_expiry", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_signed_expiry_is_rejected(invalid_expiry):
    key = compose_key(
        {"v": 1, "plan": "pro", "email": "buyer@example.com",
         "expires": invalid_expiry},
        SECRET,
    )
    with pytest.raises(LicenseError, match="invalid expiry"):
        parse_key(key)


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


def test_env_pubkey_override_is_dead_in_production(monkeypatch):
    """Regression: the env-var vendor-key override must NOT work in a shipped process.

    Simulates production by forcing the override gate False, then plays the exact
    forgery: an attacker mints their own keypair, self-signs a perpetual Pro payload,
    and points ENGRAPHIS_LICENSE_PUBKEY at their own public key. With the pinned key as
    the sole trust anchor, verification must reject it and the process stays free-tier."""
    monkeypatch.setattr(lic, "_pubkey_override_allowed", lambda: False)

    attacker_secret = b"\x99" * 32
    forged = compose_key(
        {"v": 1, "plan": "pro", "email": "atlas@plainskill.net", "expires": None},
        attacker_secret,
    )
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY",
                       ed25519_public_key(attacker_secret).hex())
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", forged)

    # vendor_public_key ignores the env entirely and returns the pinned production key
    assert lic.vendor_public_key() == bytes.fromhex(lic._VENDOR_PUBKEY_HEX)
    with pytest.raises(LicenseError, match="signature"):
        parse_key(forged)
    # end-to-end: the forged key degrades to free, not Pro
    assert current_license(refresh=True) == License.free()
    assert not has_feature("sync")


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
    with pytest.raises(LicenseError, match="engraphis.com"):
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


def test_upgrade_url_default_is_coming_soon_until_paid_available(monkeypatch):
    """Without ENGRAPHIS_PAID_AVAILABLE=1, upgrade_url routes to the informational page."""
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    monkeypatch.delenv("ENGRAPHIS_PAID_AVAILABLE", raising=False)
    assert lic.upgrade_url() == lic.DEFAULT_COMING_SOON_URL
    assert "engraphis.com" in lic.upgrade_url()


def test_upgrade_url_routes_to_polar_when_paid_available(monkeypatch):
    """With ENGRAPHIS_PAID_AVAILABLE=1, upgrade_url routes to the live checkout."""
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL", raising=False)
    monkeypatch.setenv("ENGRAPHIS_PAID_AVAILABLE", "1")
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
    # sentinel. If this fails, generate into a new secure key file and repeat the
    # reviewed rotation ceremony; never overwrite the seed that issued customer keys.
    assert lic._VENDOR_PUBKEY_HEX != lic._DEV_VENDOR_PUBKEY_HEX


def test_rotated_vendor_key_clears_the_dev_key_warning(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(b"\x09" * 32).hex())
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://buy.example.com/engraphis")
    monkeypatch.setattr(lic, "VENDOR_SIGNER_RELEASE_READY", True)
    assert lic.is_default_vendor_key() is False
    assert lic.production_warnings() == []


def test_production_signer_release_ceremony_is_pinned():
    # The 2026-07-22 Railway inventory was empty, so the reviewed release pins the
    # production seed's derived public key without a legacy compatibility verifier.
    assert lic._VENDOR_PUBKEY_HEX == (
        "77d0f9e4637bc322e494c0073b03266009a6140c7e1b99d0f47b827d4ece6d83"
    )
    assert lic._PREVIOUS_VENDOR_PUBKEY_HEXES == ()
    assert lic.VENDOR_SIGNER_RELEASE_READY is True
    assert not any("pre-sale" in warning for warning in lic.production_warnings())


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


# ── one-time trial: reset resistance + cache expiry (revenue-protection regressions) ──

def test_local_trial_file_no_longer_grants_pro(monkeypatch):
    """Bypass A regression: the retired offline trial used an HMAC derivable from public
    data (vendor public key + local machine id), so a user could forge ``trial.json`` for
    permanent Pro with no purchase and no server. Entitlement no longer reads any local
    trial file — a present/forged (even correctly-HMAC'd, far-future) trial file grants
    nothing, and ``trial_status`` is advisory only (never 'active' off a local file)."""
    monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
    payload = {"started": 1, "expires": int(time.time() + 9_000_000), "trial_days": 9999}
    lic._TRIAL_FILE.write_text(
        json.dumps({"data": payload, "sig": lic._sign_trial(payload)}))
    assert current_license(refresh=True) == License.free()
    assert not has_feature("analytics")
    assert not has_feature("sync")
    assert lic.trial_status()["active"] is False


def test_cached_license_expires_without_restart(monkeypatch):
    """Regression: the process-wide license cache must drop paid features once the
    key's expiry passes — it was immortal, so any process that outlived its key (or
    trial) kept Pro until restart."""
    now = time.time()
    key = compose_key({"v": 1, "plan": "pro", "email": "t@x.co",
                       "expires": int(now + 60)}, SECRET)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    assert current_license(refresh=True).plan == "pro"
    assert has_feature("analytics")
    monkeypatch.setattr(lic.time, "time", lambda: now + 120)  # expiry passes, NO refresh
    assert current_license().plan == "free"
    assert not has_feature("analytics")


def test_revoke_clears_cache_immediately(monkeypatch, tmp_path):
    """Regression: when the vendor relay denies a key (revoked/refunded/seat-limit),
    ``cloud_license.revalidate`` must drop the in-memory license cache so the very next
    ``current_license()`` call re-validates and falls back to free — the dead key must
    not keep granting features until its cached lease TTL expires. Driven fully offline
    by stubbing ``cloud_license.gate`` so the result is deterministic."""
    from engraphis import cloud_license
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    key = compose_key({"v": 1, "plan": "team", "email": "w@x.co", "seats": 5,
                      "issued": int(time.time()),
                      "expires": int(time.time()) + 365 * 86400}, SECRET)
    monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", key)

    # Simulate the relay accepting the key during normal operation.
    monkeypatch.setattr(cloud_license, "gate", lambda lic_obj, km, **kw: (True, ""))
    assert current_license(refresh=True).plan == "team"
    assert has_feature("team")

    # Now the relay denies the key. revalidate() contacts register() directly (not
    # gate(), which may use offline grace), so mock that authoritative denial and also
    # make the next current_license() gate fail closed.
    def _revoked(*args, **kwargs):
        raise cloud_license.Revoked("revoked")
    monkeypatch.setattr(cloud_license, "register", _revoked)
    monkeypatch.setattr(cloud_license, "gate",
                        lambda lic_obj, km, **kw: (False, "revoked"))
    assert cloud_license.revalidate(
        current_license(), key, base_url="https://lic.example") == "revoked"
    # No lingering entitlement from the stale cache — the dead key stops granting at once.
    assert current_license().plan == "free"
    assert not has_feature("team")


def test_vendor_cli_issues_for_license_host_and_records_inventory(
        monkeypatch, tmp_path, capsys):
    from scripts import license_admin
    from engraphis.inspector.license_registry import inventory

    key_file = tmp_path / "signer.key"
    registry = tmp_path / "relay.db"
    key_file.write_text(SECRET.hex(), encoding="utf-8")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(SECRET).hex())
    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(registry))
    monkeypatch.delenv("ENGRAPHIS_CLOUD_URL", raising=False)

    license_admin.main([
        "issue", "--email", "buyer@example.com", "--plan", "pro",
        "--days", "30", "--key-file", str(key_file),
    ])
    key = capsys.readouterr().out.strip()
    parsed = parse_key(key)
    assert parsed.cloud_url == "https://license.engraphis.com"
    payload = json.loads(lic._b64u_decode(key.split(".")[1]).decode("utf-8"))
    assert payload["signing_key_id"] == ed25519_public_key(SECRET).hex()[:16]
    assert inventory(str(registry)) == {
        "issued_total": 1,
        "active": 1,
        "revoked": 0,
        "other_status": 0,
        "plans": {"pro": 1},
        "active_key_ids": [parsed.key_id],
        "signing_key_ids": {ed25519_public_key(SECRET).hex()[:16]: 1},
        "registered_machines": 0,
        "rotation_reissues": 0,
        "rotation_requires_migration": True,
    }


def test_registry_migrates_old_rows_and_marks_unknown_signers(tmp_path):
    from engraphis.inspector.license_registry import connect, inventory

    registry = tmp_path / "legacy-registry.db"
    conn = sqlite3.connect(str(registry))
    conn.execute(
        "CREATE TABLE issued_licenses (key_id TEXT PRIMARY KEY,email TEXT,plan TEXT,"
        "seats INTEGER,issued REAL,expires REAL,subscription_id TEXT,order_id TEXT,"
        "status TEXT NOT NULL DEFAULT 'active',created_at REAL NOT NULL,revoked_at REAL)")
    conn.execute(
        "INSERT INTO issued_licenses(key_id,plan,status,created_at) VALUES(?,?,?,?)",
        ("legacy-key", "team", "active", time.time()))
    conn.commit()
    conn.close()

    migrated = connect(str(registry))
    columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(issued_licenses)").fetchall()}
    migrated.close()

    assert "signing_key_id" in columns
    assert inventory(str(registry))["signing_key_ids"] == {"unknown": 1}


def test_rotation_reissue_preserves_payload_and_retires_only_after_grace(
        monkeypatch, tmp_path, capsys):
    from scripts import license_admin
    from engraphis.inspector import license_registry as registry

    old_secret = b"\x31" * 32
    new_secret = b"\x32" * 32
    old_public = ed25519_public_key(old_secret).hex()
    new_public = ed25519_public_key(new_secret).hex()
    monkeypatch.setattr(lic, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    monkeypatch.setattr(lic, "_VENDOR_PUBKEY_HEX", new_public)
    monkeypatch.setattr(lic, "_PREVIOUS_VENDOR_PUBKEY_HEXES", (old_public,))

    now = int(time.time())
    original_payload = {
        "v": 1,
        "plan": "team",
        "email": "rotation-buyer@example.com",
        "seats": 7,
        "issued": now - 86400,
        "expires": now + 90 * 86400,
        "features": ["contract-export"],
        "enforce": "cloud",
        "cloud_url": "https://license.engraphis.com",
        "subscription_id": "sub_rotation",
        "order_id": "order_rotation",
        "signing_key_id": old_public[:16],
        "future_contract_field": {"preserve": True},
    }
    old_key = compose_key(original_payload, old_secret)
    database = tmp_path / "relay.db"
    registry.record_issued(old_key, db_path=str(database))
    source_file = tmp_path / "active-keys.txt"
    source_file.write_text(old_key + "\n", encoding="utf-8")
    new_seed_file = tmp_path / "new-signer.key"
    new_seed_file.write_text(new_secret.hex() + "\n", encoding="utf-8")
    output_file = tmp_path / "replacement-keys.json"
    command = [
        "rotation-reissue",
        "--db-path", str(database),
        "--source-file", str(source_file),
        "--new-key-file", str(new_seed_file),
        "--output-file", str(output_file),
    ]

    license_admin.main(command)
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["applied"] is False
    assert dry_run["reissued"] == 1
    assert not output_file.exists()
    assert registry.inventory(str(database))["active"] == 1

    license_admin.main(command + ["--apply"])
    applied_text = capsys.readouterr().out
    applied = json.loads(applied_text)
    assert applied["applied"] is True
    assert applied["registry_reissues_recorded"] == 1
    assert old_key not in applied_text
    assert "rotation-buyer@example.com" not in applied_text

    manifest = json.loads(output_file.read_text(encoding="utf-8"))
    replacement_key = manifest["reissues"][0]["license_key"]
    replacement_payload = json.loads(
        lic._b64u_decode(replacement_key.split(".")[1]).decode("utf-8"))
    expected_payload = dict(original_payload)
    expected_payload["signing_key_id"] = new_public[:16]
    assert replacement_payload == expected_payload
    assert parse_key(old_key).signing_key_id == old_public[:16]
    replacement = parse_key(replacement_key)
    assert replacement.signing_key_id == new_public[:16]
    assert replacement.expires == original_payload["expires"]
    assert replacement.subscription_id == "sub_rotation"
    assert replacement.order_id == "order_rotation"

    rotation_inventory = registry.inventory(str(database))
    assert rotation_inventory["active"] == 2
    assert rotation_inventory["rotation_reissues"] == 1
    assert rotation_inventory["signing_key_ids"] == {
        old_public[:16]: 1,
        new_public[:16]: 1,
    }
    assert registry.is_revoked(parse_key(old_key, now=0).key_id, db_path=str(database)) is False
    assert registry.is_revoked(replacement.key_id, db_path=str(database)) is False

    retire_command = [
        "rotation-retire",
        "--db-path", str(database),
        "--manifest-file", str(output_file),
    ]
    license_admin.main(retire_command)
    retirement_dry_run = json.loads(capsys.readouterr().out)
    assert retirement_dry_run["applied"] is False
    with pytest.raises(SystemExit, match="30-day"):
        license_admin.main(retire_command + ["--confirm-activated", "--apply"])

    state = registry.signer_rotation_state(new_public[:16], db_path=str(database))
    audit_created_at = state["completed"][0]["created_at"]
    monkeypatch.setattr(registry.time, "time", lambda: audit_created_at + 30 * 86400 + 1)
    license_admin.main(retire_command + ["--confirm-activated", "--apply"])
    retired = json.loads(capsys.readouterr().out)
    assert retired["applied"] is True
    assert retired["revoked"] == 1
    assert registry.is_revoked(parse_key(old_key, now=0).key_id, db_path=str(database)) is True
    assert registry.is_revoked(replacement.key_id, db_path=str(database)) is False


def test_rotation_reissue_refuses_incomplete_active_key_inventory(
        monkeypatch, tmp_path):
    from scripts import license_admin
    from engraphis.inspector import license_registry as registry

    old_secret = b"\x41" * 32
    new_secret = b"\x42" * 32
    old_public = ed25519_public_key(old_secret).hex()
    new_public = ed25519_public_key(new_secret).hex()
    monkeypatch.setattr(lic, "_TEST_MODE_PUBKEY_OVERRIDE", False)
    monkeypatch.setattr(lic, "_VENDOR_PUBKEY_HEX", new_public)
    monkeypatch.setattr(lic, "_PREVIOUS_VENDOR_PUBKEY_HEXES", (old_public,))

    database = tmp_path / "relay.db"
    now = int(time.time())
    keys = []
    for index in range(2):
        payload = {
            "v": 1,
            "plan": "pro",
            "email": f"buyer-{index}@example.com",
            "seats": 1,
            "issued": now,
            "expires": now + 30 * 86400,
            "signing_key_id": old_public[:16],
        }
        key = compose_key(payload, old_secret)
        registry.record_issued(key, db_path=str(database))
        keys.append(key)

    source_file = tmp_path / "incomplete-keys.txt"
    source_file.write_text(keys[0] + "\n", encoding="utf-8")
    seed_file = tmp_path / "new-signer.key"
    seed_file.write_text(new_secret.hex() + "\n", encoding="utf-8")
    output_file = tmp_path / "replacements.json"
    with pytest.raises(SystemExit, match="missing registry key ids"):
        license_admin.main([
            "rotation-reissue",
            "--db-path", str(database),
            "--source-file", str(source_file),
            "--new-key-file", str(seed_file),
            "--output-file", str(output_file),
            "--apply",
        ])

    assert not output_file.exists()
    post_failure = registry.inventory(str(database))
    assert post_failure["active"] == 2
    assert post_failure["rotation_reissues"] == 0
