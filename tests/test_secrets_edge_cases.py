"""Reliability edge cases for the capture-time secret boundary."""

from engraphis.core.secrets import secret_kind


def test_secret_detection_handles_cyclic_metadata_without_recursing_forever():
    metadata = {}
    metadata["self"] = metadata

    assert secret_kind(metadata) is None


def test_secret_detection_still_finds_credentials_beside_a_cycle():
    metadata = {}
    metadata["self"] = metadata
    metadata["api_key"] = "credential-value-123456"

    assert secret_kind(metadata) == "credential assignment"
