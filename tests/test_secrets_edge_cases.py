"""Reliability edge cases for the capture-time secret boundary."""

from engraphis.core.secrets import redact_secrets, secret_kind


def test_secret_detection_handles_cyclic_metadata_without_recursing_forever():
    metadata = {}
    metadata["self"] = metadata

    assert secret_kind(metadata) is None


def test_secret_detection_still_finds_credentials_beside_a_cycle():
    metadata = {}
    metadata["self"] = metadata
    metadata["api_key"] = "credential-value-123456"

    assert secret_kind(metadata) == "credential assignment"


def test_redaction_removes_credential_values_without_weakening_write_rejection():
    secret = "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    redacted = redact_secrets(f"provider_key={secret}")

    assert secret not in redacted
    assert secret_kind(redacted) is None
    assert redact_secrets(redacted) == redacted


def test_redaction_removes_an_entire_pem_private_key_block():
    private_key = "-----BEGIN PRIVATE KEY-----\nabc123secret\n-----END PRIVATE KEY-----"
    redacted = redact_secrets(private_key)

    assert private_key not in redacted
    assert "abc123secret" not in redacted
    assert secret_kind(redacted) is None


def test_redaction_linear_time_on_repeated_pem_headers():
    import time

    malicious = "-----BEGIN PRIVATE KEY----- " * 1000
    t0 = time.time()
    res = redact_secrets(malicious)
    elapsed = time.time() - t0
    assert elapsed < 1.0
    assert isinstance(res, str)

