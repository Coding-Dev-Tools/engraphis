"""E2E: issue a key through the webhook pipeline and verify with parse_key."""
import os, secrets, tempfile
from engraphis.inspector.webhooks import issue_key
from engraphis.licensing import parse_key, ed25519_public_key


def test_issue_and_verify_roundtrip(monkeypatch):
    sk = secrets.token_bytes(32)
    pub = ed25519_public_key(sk).hex()

    f = tempfile.NamedTemporaryFile(mode="w", suffix=".key", delete=False)
    f.write(sk.hex())
    f.close()
    monkeypatch.setenv("ENGRAPHIS_SIGNING_KEY", f.name)
    # Must also override the verify key so parse_key uses the matching public key
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", pub)

    try:
        key = issue_key("test@example.com", product_name="Engraphis Pro Monthly", seats=3, days=30)
        lic = parse_key(key)
        assert lic.plan == "pro", f"expected pro, got {lic.plan}"
        assert lic.email == "test@example.com"
        assert lic.seats == 3
        assert "analytics" in lic.features
        assert lic.expires is not None
    finally:
        os.unlink(f.name)
