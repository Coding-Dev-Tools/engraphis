"""Universal, offline-first document import orchestration for Engraphis v2.

The format adapters and secure filesystem walk live in :mod:`engraphis.core.documents`.
This module binds their source-neutral records to the already-proven temporal import
planner, per-document transaction finalizer, manifest, graph, jobs, and receipts.
Obsidian remains a compatibility adapter, not the persistence model.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import re
from typing import Any, Iterable, Optional

from engraphis.core.documents import (
    IMPORTER_VERSION,
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_FILES,
    MAX_DOCUMENT_TREE_BYTES,
    SENSITIVE_FILENAMES,
    DocumentFileIssue,
    DocumentRecord,
    DocumentScan,
    document_format_for_path,
    normalize_document_path,
    parse_document,
)
from engraphis.obsidian_import import ObsidianImportCancelled, ObsidianImporter


DocumentImportCancelled = ObsidianImportCancelled


def local_document_adapter(
    raw: bytes, relative_path: str, source_mtime_ns: Optional[int] = None,
) -> DocumentRecord:
    """Use installed v2 local resource adapters without crossing into ``core``.

    PDF and OCR adapters are fully local. Audio/video transcription additionally
    requires ``ENGRAPHIS_WHISPER_MODEL`` to name an existing local path, preventing
    the backend library from resolving a model name over the network.
    """
    spec = document_format_for_path(relative_path)
    if spec is None or not spec.requires_adapter:
        raise ValueError("document format does not require a local adapter")
    if spec.name in {"audio", "video"}:
        model_path = os.environ.get("ENGRAPHIS_WHISPER_MODEL", "").strip()
        selected_model = Path(model_path).expanduser() if model_path else None
        if not selected_model or not (
            selected_model.is_file() or selected_model.is_dir()
        ):
            raise ValueError(
                "transcription requires an existing local model file or directory"
            )
    # Concrete backend composition deliberately stays outside engraphis/core/.
    from engraphis.backends.resources import ResourceExtractionError, get_resource_extractor

    try:
        resource = get_resource_extractor().extract_bytes(relative_path, raw)
    except ResourceExtractionError as exc:
        text = str(exc).casefold()
        if "needs" in text or "requires" in text:
            raise ValueError("optional local document extractor is unavailable") from None
        raise ValueError(_safe_reason(exc)) from None
    readable = str(resource.text or "").strip()
    if not readable:
        raise ValueError("document produced no readable text")
    return DocumentRecord(
        relative_path=relative_path, format=spec.name,
        media_type=str(resource.media_type or spec.media_type),
        title=str(resource.title or Path(relative_path).stem)[:300],
        content=readable, body=readable,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_sha256=hashlib.sha256(readable.encode("utf-8")).hexdigest(),
        source_size=len(raw), source_mtime_ns=source_mtime_ns,
        title_source="extracted",
        metadata={"resource_kind": resource.kind, **dict(resource.metadata or {})},
        warnings=[str(value)[:500] for value in list(resource.warnings or [])[:100]],
    )


def _sensitive_filename(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in SENSITIVE_FILENAMES
        or lowered.startswith(".env.")
        or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
        or bool(re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", lowered))
    )


def _safe_reason(exc: BaseException) -> str:
    text = str(exc).casefold()
    for label in (
        "secret", "safety limit", "unsupported", "invalid", "unsafe", "binary",
        "too large", "no readable text",
    ):
        if label in text:
            return f"source rejected: {label}"
    return "source rejected"


def scan_document_upload(
    files: Iterable[tuple[str, bytes]], *, source_label: str,
) -> DocumentScan:
    """Parse browser-selected document bytes without creating an upload copy."""
    label = str(source_label or "").strip()[:200]
    if not label:
        raise ValueError("source_label is required for a browser source")
    source_id = hashlib.sha256(
        ("documents-browser\0" + label).encode("utf-8", "surrogatepass")
    ).hexdigest()
    scan = DocumentScan(root_path="", source_id=source_id)
    total = 0
    seen: set[str] = set()
    for index, (raw_path, raw) in enumerate(files):
        if index >= MAX_DOCUMENT_FILES:
            scan.rejected.append(DocumentFileIssue(
                "(collection)", "source exceeds document file safety limit",
            ))
            break
        try:
            relative_path = normalize_document_path(raw_path)
        except ValueError:
            scan.rejected.append(DocumentFileIssue("(invalid path)", "invalid source path"))
            continue
        parts = PurePosixPath(relative_path).parts
        if any(part.startswith(".") for part in parts):
            scan.skipped.append(DocumentFileIssue(
                relative_path, "hidden/configuration path skipped",
            ))
            continue
        portable_path = relative_path.casefold()
        if portable_path in seen:
            scan.rejected.append(DocumentFileIssue(relative_path, "duplicate upload path"))
            continue
        seen.add(portable_path)
        if document_format_for_path(relative_path) is None:
            scan.skipped.append(DocumentFileIssue(relative_path, "unsupported document format"))
            continue
        if _sensitive_filename(parts[-1]):
            scan.rejected.append(DocumentFileIssue(relative_path, "sensitive filename"))
            continue
        if not isinstance(raw, bytes):
            scan.rejected.append(DocumentFileIssue(relative_path, "invalid upload"))
            continue
        if len(raw) > MAX_DOCUMENT_BYTES:
            scan.rejected.append(DocumentFileIssue(
                relative_path, "document exceeds byte safety limit",
            ))
            continue
        total += len(raw)
        if total > MAX_DOCUMENT_TREE_BYTES:
            scan.rejected.append(DocumentFileIssue(
                relative_path, "source exceeds total byte safety limit",
            ))
            break
        try:
            scan.documents.append(parse_document(
                raw, relative_path, adapter=local_document_adapter,
            ))
        except ValueError as exc:
            scan.rejected.append(DocumentFileIssue(relative_path, _safe_reason(exc)))
    return scan


def _bounded_source_metadata(value: Any) -> tuple[dict[str, Any], int]:
    """Return a small JSON-safe format metadata object with deterministic bounds."""
    if not isinstance(value, dict):
        return {}, 0
    result: dict[str, Any] = {}
    for raw_key, raw_value in list(value.items())[:64]:
        key = str(raw_key)[:80]
        if isinstance(raw_value, (str, int, float, bool)) or raw_value is None:
            result[key] = raw_value if not isinstance(raw_value, str) else raw_value[:1000]
        elif isinstance(raw_value, (list, tuple)):
            result[key] = [str(item)[:300] for item in list(raw_value)[:100]]
        elif isinstance(raw_value, dict):
            result[key] = {
                str(child_key)[:80]: str(child_value)[:300]
                for child_key, child_value in list(raw_value.items())[:32]
            }
    while result and len(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ) > 4_000:
        result.pop(next(reversed(result)))
    return result, max(0, len(value) - len(result))


class DocumentImporter(ObsidianImporter):
    """Source-neutral temporal importer for mixed local document collections."""

    SOURCE_KIND = "documents"
    JOB_KIND = "document_import"
    RECEIPT_OPERATION = "document_import"
    CLAIM_KIND = "source_document"
    SUBJECT_PREFIX = "document"
    METADATA_KEY = "document"
    DEFAULT_LABEL = "Document collection"
    IMPORTER_VERSION = IMPORTER_VERSION
    COUNT_KEY = "documents"
    LINK_REASON = "document_reference"
    LINK_IMPORTED_ATTACHMENTS = True

    def preview(
        self, scan: DocumentScan, *, source_id: Optional[str] = None,
        source_label: str = "", **kwargs: Any,
    ) -> dict:
        vault_id = kwargs.pop("vault_id", None)
        vault_label = kwargs.pop("vault_label", "")
        return super().preview(
            scan, vault_id=source_id or vault_id,
            vault_label=source_label or vault_label, **kwargs,
        )

    def import_scan(
        self, scan: DocumentScan, *, source_id: Optional[str] = None,
        source_label: str = "", **kwargs: Any,
    ) -> dict:
        vault_id = kwargs.pop("vault_id", None)
        vault_label = kwargs.pop("vault_label", "")
        return super().import_scan(
            scan, vault_id=source_id or vault_id,
            vault_label=source_label or vault_label, **kwargs,
        )

    def prepare_import(
        self, scan: DocumentScan, *, source_id: Optional[str] = None,
        source_label: str = "", **kwargs: Any,
    ) -> dict:
        vault_id = kwargs.pop("vault_id", None)
        vault_label = kwargs.pop("vault_label", "")
        return super().prepare_import(
            scan, vault_id=source_id or vault_id,
            vault_label=source_label or vault_label, **kwargs,
        )

    @classmethod
    def _metadata(
        cls, note: DocumentRecord, *, vault_id: str, source_id: str,
        imported_at: float, actor: str, branch: str,
    ) -> dict:
        envelope = super()._metadata(
            note, vault_id=vault_id, source_id=source_id,
            imported_at=imported_at, actor=actor, branch=branch,
        )
        document = envelope[cls.METADATA_KEY]
        document["format"] = str(note.format)[:64]
        document["media_type"] = str(note.media_type)[:200]
        format_metadata, pre_omitted = _bounded_source_metadata(note.metadata)
        original_format_keys = len(format_metadata)
        document["format_metadata"] = format_metadata
        document["original_title"] = str(note.title)[:1000]
        # The base envelope is already bounded near the Store ceiling. Format
        # adapters can add another 4 KiB, so trim their least-significant tail
        # against the complete envelope rather than allowing a valid document to
        # fail only when the canonical memory is written.
        while format_metadata and len(
            json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ) > 14_000:
            format_metadata.pop(next(reversed(format_metadata)))
        omitted = pre_omitted + original_format_keys - len(format_metadata)
        if omitted:
            counts = document.setdefault("omitted_counts", {})
            counts["format_metadata"] = int(counts.get("format_metadata", 0)) + omitted
        return envelope


__all__ = [
    "DocumentImportCancelled", "DocumentImporter", "local_document_adapter",
    "scan_document_upload",
]
