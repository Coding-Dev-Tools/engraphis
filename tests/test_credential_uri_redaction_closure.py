"""Synthetic nested URI credentials stay safe after capture and diagnostic redaction."""
from __future__ import annotations

import json
import time

import pytest

from engraphis.core.secrets import _credential_uri_spans, redact_secrets, secret_kind


NESTED = "x://u:password@y://u2:password2@h"
SAFE = "<redacted><redacted>h"


@pytest.mark.parametrize(("text", "expected"), [
    (NESTED, SAFE),
    ("prefix " + NESTED + " suffix", "prefix " + SAFE + " suffix"),
    ("(" + NESTED + "), next", "(" + SAFE + "), next"),
    ('"' + NESTED + '"', '"' + SAFE + '"'),
    ("x://u:p@y://u:q@z://u:r@host/path?keep=1#fragment",
     "<redacted><redacted><redacted>host/path?keep=1#fragment"),
    ("x://u:p@İ://u:q@h", "<redacted><redacted>h"),
    ("x://u:p@custom+mail-2.0://:q@h", "<redacted><redacted>h"),
    ("x://u:p@y://f'ix:p'ass@h", "<redacted><redacted>h"),
    ("x://u:p@y://u:p%2Fq@h", "<redacted><redacted>h"),
    (NESTED + "\u2003" + NESTED, SAFE + "\u2003" + SAFE),
    (NESTED + "?next=" + NESTED, SAFE + "?next=" + SAFE),
    ("<redacted>" + NESTED, "<redacted>" + SAFE),
    ("x://u:p@y://u:<redacted>@h", "<redacted>y://u:<redacted>@h"),
    ("x://u:p@y://u:withheld@h", "<redacted>y://u:withheld@h"),
    ("x://u:p@1smtp://u:q@h", "<redacted>1smtp://u:q@h"),
    ("x://u:p@y://u@h", "<redacted>y://u@h"),
])
def test_nested_redaction_is_complete_idempotent_and_keeps_context(text, expected):
    assert secret_kind(text) == "credential-bearing connection URI"
    safe = redact_secrets(text)
    assert safe == expected
    assert secret_kind(safe) is None
    assert redact_secrets(safe) == safe


@pytest.mark.parametrize("text", [
    "a://smtp://u:p@host.invalid", "1smtp://u:p@host.invalid",
    "+smtp://u:p@host.invalid", "https://example.invalid/path/u:p@host",
    "smtp://user@host.invalid", "smtp://user:@host.invalid",
    "smtp://u:<redacted>@h", "smtp://u:withheld@h",
    "smtp://u:removed@h", "smtp://u:not-set@h",
    "Use password rotation and document userinfo separately.",
    "ordinary https://example.invalid:443/path?keep=1#fragment",
])
def test_detector_and_nonsecret_text_contracts_stay_unchanged(text):
    assert list(_credential_uri_spans(text)) == []
    assert secret_kind(text) is None
    assert redact_secrets(text) == text


def test_many_newly_exposed_hosts_do_not_require_whole_text_rescans():
    depth = 10_000
    text = "x://u:p@" * depth + "host.invalid/path?keep=1"
    started = time.process_time()
    safe = redact_secrets(text)
    assert safe == "<redacted>" * depth + "host.invalid/path?keep=1"
    assert redact_secrets(safe) == safe and secret_kind(safe) is None
    assert time.process_time() - started < 3


@pytest.mark.parametrize("boundary", ["service", "engine"])
def test_opted_in_capture_persists_only_complete_redaction(boundary):
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.secrets import SecretDetectedError
    from engraphis.service import MemoryService, ValidationError

    instance = (MemoryService.create(":memory:", graph_extractor="none")
                if boundary == "service" else MemoryEngine.create(":memory:"))
    try:
        if boundary == "service":
            with pytest.raises(ValidationError, match="credential-bearing connection URI"):
                instance.remember(NESTED, workspace="synthetic")
            assert instance.store.count_memories() == 0
            result = instance.remember(NESTED, title=NESTED, workspace="synthetic", redact_secrets=True)
            record = instance.store.get_memory(result["id"])
        else:
            workspace = instance.store.get_or_create_workspace("synthetic")
            with pytest.raises(SecretDetectedError):
                instance.remember(NESTED, workspace_id=workspace)
            assert instance.store.count_memories() == 0
            memory_id = instance.remember(NESTED, title=NESTED, workspace_id=workspace,
                                          redact_secrets=True, resolve_conflicts=False)
            record = instance.store.get_memory(memory_id)
        assert record is not None and record.content == SAFE and record.title == SAFE
        assert secret_kind(record.content) is None
    finally:
        if boundary == "service":
            instance.close()
        else:
            instance.store.close()


def _locomo(path, content):
    path.write_text(json.dumps([{
        "sample_id": "synthetic-case", "conversation": {
            "session_1_date_time": "2026-10-18", "session_1": [
                {"speaker": "A", "dia_id": "D1:1", "text": content}]},
        "qa": [{"question": "Which host?", "answer": "h", "evidence": ["D1:1"], "category": 1}],
    }]), encoding="utf-8")
    return path


@pytest.mark.parametrize("format", ["locomo", "longmemeval"])
def test_external_dataset_loader_returns_capture_safe_memories(tmp_path, format):
    from eval import external

    path = tmp_path / "synthetic.json"
    if format == "locomo":
        _locomo(path, "Context " + NESTED + " retained.")
        cases = external.load_locomo(str(path))
    else:
        path.write_text(json.dumps([{
            "question_id": "synthetic", "question": "Which host?", "answer": "h",
            "haystack_session_ids": ["s1"], "haystack_dates": ["2026-10-18"],
            "haystack_sessions": [[{"role": "user", "content": "Context " + NESTED + " retained."}]],
            "answer_session_ids": ["s1"],
        }]), encoding="utf-8")
        cases = external.load_longmemeval(str(path))
    memory = cases[0]["memories"][0]["text"]
    assert "Context " + SAFE + " retained." in memory
    assert secret_kind(memory) is None and redact_secrets(memory) == memory


@pytest.mark.parametrize("failure", ["read", "count", "artifact"])
def test_external_diagnostic_errors_never_emit_newly_exposed_passwords(
    tmp_path, monkeypatch, capsys, failure,
):
    from eval import external

    dataset = _locomo(tmp_path / "synthetic.json", "The release marker is indigo.")

    def failed(*args, **kwargs):
        raise ValueError("synthetic failure before " + NESTED + " after")

    monkeypatch.setattr(external, {"read": "_read_json_snapshot", "count": "source_case_count",
                                  "artifact": "write_canonical_artifact"}[failure], failed)
    argv = ["--dataset", str(dataset), "--format", "locomo", "--offline"]
    if failure == "artifact":
        argv += ["--artifact", str(tmp_path / "synthetic-artifact.json")]
    assert external.main(argv) == 2
    logged = capsys.readouterr().err
    assert "synthetic failure before " + SAFE + " after" in logged
    assert "password" not in logged and secret_kind(logged) is None
