"""Regression coverage for safe import-files error responses.

Import inputs and extractors can include sensitive local data in exception messages.  The
service must retain its stable report contract without reflecting those messages.
"""
from types import SimpleNamespace

from engraphis.backends import resources
from engraphis.service import MemoryService, ValidationError


_REPORT_KEYS = {
    "workspace", "scanned", "imported", "skipped", "errors", "derived_facts",
    "details", "warnings",
}


def _assert_report_shape(report):
    assert set(report) == _REPORT_KEYS
    assert report["workspace"] == "redaction"
    assert report["scanned"] == 1
    assert isinstance(report["details"], list)
    assert isinstance(report["warnings"], list)


def test_import_files_redacts_validation_error_from_memory_write(monkeypatch):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    secret = "VALIDATION_IMPORT_SECRET_must_not_escape"

    def reject_write(*_args, **_kwargs):
        raise ValidationError(secret)

    monkeypatch.setattr(svc, "remember", reject_write)
    report = svc.import_files(
        workspace="redaction", files=[{"name": "note.md", "content": "note"}]
    )

    _assert_report_shape(report)
    assert report["imported"] == 0
    assert report["skipped"] == 0
    assert report["errors"] == 1
    assert report["derived_facts"] == 0
    assert report["details"] == [{"file": "note.md", "error": "resource could not be imported"}]
    assert secret not in repr(report)


def test_import_files_redacts_resource_extractor_value_error(monkeypatch):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    secret = "EXTRACTOR_IMPORT_SECRET_must_not_escape"

    def fail_extract(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(
        resources,
        "get_resource_extractor",
        lambda: SimpleNamespace(extract_bytes=fail_extract),
    )
    report = svc.import_files(
        workspace="redaction", files=[{"name": "note.md", "content": "note"}]
    )

    _assert_report_shape(report)
    assert report["imported"] == 0
    assert report["skipped"] == 0
    assert report["errors"] == 1
    assert report["derived_facts"] == 0
    assert report["details"] == [{"file": "note.md", "error": "resource could not be imported"}]
    assert secret not in repr(report)


def test_import_files_redacts_derive_facts_value_error(monkeypatch):
    svc = MemoryService.create(":memory:", graph_extractor="none")
    secret = "DERIVE_FACTS_IMPORT_SECRET_must_not_escape"

    def fail_derivation(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(svc, "_derive_import_facts", fail_derivation)
    report = svc.import_files(
        workspace="redaction",
        files=[{"name": "note.md", "content": "note"}],
        derive_facts=True,
    )

    _assert_report_shape(report)
    assert report["imported"] == 1
    assert report["skipped"] == 0
    assert report["errors"] == 0
    assert report["derived_facts"] == 0
    assert report["details"] == []
    assert report["warnings"] == [{"file": "note.md", "warnings": ["fact derivation failed"]}]
    assert secret not in repr(report)
