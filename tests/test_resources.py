import hashlib
import io
import os
import sys
import types
import traceback
import zipfile

import pytest

from engraphis.backends import resources
from engraphis.backends.resources import (
    LocalResourceExtractor,
    ResourceExtractionError,
)
from engraphis.service import MemoryService


def _docx_bytes(text: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


def test_stdlib_resource_extractors_cover_html_docx_and_code():
    extractor = LocalResourceExtractor()
    html = extractor.extract_bytes(
        "page.html", b"<h1>Title</h1><script>ignore()</script><p>Hello world</p>"
    )
    assert html.title == "Title" and "ignore" not in html.text

    docx = extractor.extract_bytes("notes.docx", _docx_bytes("Deployment notes"))
    assert docx.text == "Deployment notes" and docx.metadata["paragraphs"] == 1

    code = extractor.extract_bytes("app.py", b"def run():\n    return 1\n")
    assert code.kind == "code" and "def run" in code.text


def test_unknown_binary_resource_fails_actionably():
    with pytest.raises(ResourceExtractionError):
        LocalResourceExtractor().extract_bytes("blob.bin", b"\x00\x01\x02\x03" * 100)


def test_extract_path_rejects_oversized_media_before_transcription(tmp_path, monkeypatch):
    def unexpected_transcription(*_args, **_kwargs):
        raise AssertionError("oversized resource reached transcription")

    monkeypatch.setattr(resources, "MAX_RESOURCE_BYTES", 1)
    monkeypatch.setattr(resources, "_transcribe_path", unexpected_transcription)
    extractor = LocalResourceExtractor()

    for suffix in (".mp3", ".mp4"):
        path = tmp_path / f"oversized{suffix}"
        path.write_bytes(b"xx")

        with pytest.raises(ResourceExtractionError) as exc_info:
            extractor.extract_path(str(path))

        assert str(exc_info.value) == "resource exceeds the 1-byte extraction limit"


def test_extract_path_transcribes_and_hashes_the_same_snapshot(tmp_path, monkeypatch):
    payload = b"stable-media-snapshot"
    source = tmp_path / "recording.mp3"
    source.write_bytes(payload)
    transcribed = {}

    def transcribe(path):
        with open(path, "rb") as stream:
            transcribed["bytes"] = stream.read()
        return "stable transcript", {"duration": 1.0}

    monkeypatch.setattr(resources, "_transcribe_path", transcribe)

    document = LocalResourceExtractor().extract_path(str(source))

    assert transcribed["bytes"] == payload
    assert document.metadata["resource_bytes"] == len(payload)
    assert document.metadata["resource_sha256"] == hashlib.sha256(payload).hexdigest()


def test_extract_path_rejects_a_regular_file_swap_before_open(tmp_path, monkeypatch):
    source = tmp_path / "resource.txt"
    replacement = tmp_path / "replacement.txt"
    source.write_text("original", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")
    original_open = resources.os.open

    def swap_then_open(path, flags):
        os.replace(replacement, source)
        return original_open(path, flags)

    monkeypatch.setattr(resources.os, "open", swap_then_open)

    with pytest.raises(ResourceExtractionError, match="changed before it was opened"):
        LocalResourceExtractor().extract_path(str(source))


def test_docx_rejects_dtd_and_entity_declarations():
    xml = (
        '<?xml version="1.0"?>' + (" " * 5_000)
        + '<!DOCTYPE x [<!ENTITY payload "unsafe">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>&payload;</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)

    with pytest.raises(ResourceExtractionError, match="entities are not allowed"):
        LocalResourceExtractor().extract_bytes("unsafe.docx", buf.getvalue())


def _assert_redacted_failure(call, expected: str, marker: str):
    with pytest.raises(ResourceExtractionError, match=expected) as exc_info:
        call()
    rendered = "".join(
        traceback.format_exception(
            type(exc_info.value),
            exc_info.value,
            exc_info.value.__traceback__,
        )
    )
    assert marker not in str(exc_info.value)
    assert marker not in repr(exc_info.value)
    assert marker not in rendered


def test_docx_parser_error_is_redacted():
    marker = "C:/private/customer/source.docx"
    _assert_redacted_failure(
        lambda: LocalResourceExtractor().extract_bytes(marker, b"not-a-zip"),
        "invalid DOCX archive",
        marker,
    )


def test_pdf_parser_error_is_redacted(monkeypatch):
    marker = "signed-pdf-url-token"

    class _Reader:
        def __init__(self, _stream):
            raise RuntimeError(marker)

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=_Reader))
    _assert_redacted_failure(
        lambda: resources._pdf_text(b"%PDF-fake"),
        "PDF extraction failed",
        marker,
    )


def test_image_parser_error_is_redacted(monkeypatch):
    marker = "C:/private/customer/image.png"

    class _Image:
        @staticmethod
        def open(_stream):
            raise RuntimeError(marker)

    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=_Image))
    monkeypatch.setitem(sys.modules, "pytesseract", types.SimpleNamespace())
    _assert_redacted_failure(
        lambda: resources._image_text(b"not-an-image"),
        "image OCR failed",
        marker,
    )


def test_image_ocr_output_error_is_redacted(monkeypatch):
    marker = "ocr-provider-secret"

    class _OpenedImage:
        width = 1
        height = 1
        format = "PNG"

    class _Image:
        @staticmethod
        def open(_stream):
            return _OpenedImage()

    class _OCRText:
        def __str__(self):
            raise RuntimeError(marker)

    class _OCR:
        @staticmethod
        def image_to_string(_image):
            return _OCRText()

    monkeypatch.setitem(sys.modules, "PIL", types.SimpleNamespace(Image=_Image))
    monkeypatch.setitem(sys.modules, "pytesseract", _OCR())
    _assert_redacted_failure(
        lambda: resources._image_text(b"not-an-image"),
        "image OCR failed",
        marker,
    )


def test_transcription_error_is_redacted(monkeypatch):
    marker = "super-secret-model-path"

    class _WhisperModel:
        def __init__(self, *_args, **_kwargs):
            raise RuntimeError(marker)

    monkeypatch.setenv("ENGRAPHIS_WHISPER_MODEL", "configured-model")
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=_WhisperModel),
    )
    _assert_redacted_failure(
        lambda: resources._transcribe_path("media.mp3"),
        "transcription failed",
        marker,
    )


def test_transcription_metadata_error_is_redacted(monkeypatch):
    marker = "transcription-provider-secret"

    class _Info:
        language = "en"
        language_probability = marker
        duration = 1.0

    class _WhisperModel:
        def __init__(self, *_args, **_kwargs):
            pass

        def transcribe(self, *_args, **_kwargs):
            return [], _Info()

    monkeypatch.setenv("ENGRAPHIS_WHISPER_MODEL", "configured-model")
    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=_WhisperModel),
    )
    _assert_redacted_failure(
        lambda: resources._transcribe_path("media.mp3"),
        "transcription failed",
        marker,
    )


def test_pdf_extraction_bounds_pages_and_text(monkeypatch):
    class _Page:
        def __init__(self, text):
            self.text = text

        def extract_text(self):
            return self.text

    class _Reader:
        def __init__(self, _stream):
            self.pages = [_Page("first"), _Page("second"), _Page("third")]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=_Reader))
    monkeypatch.setattr(resources, "MAX_PDF_PAGES", 2)
    monkeypatch.setattr(resources, "MAX_EXTRACTED_TEXT_CHARS", 8)

    result = LocalResourceExtractor().extract_bytes("bounded.pdf", b"%PDF-fake")

    assert result.text == "first\n\ns"
    assert result.metadata["pages"] == 3
    assert result.metadata["pages_extracted"] == 2
    assert any("first 2 of 3 pages" in warning for warning in result.warnings)
    assert any("truncated to 8 characters" in warning for warning in result.warnings)


def test_import_files_accepts_bytes_and_preserves_resource_provenance():
    svc = MemoryService.create(":memory:", graph_extractor="none")
    out = svc.import_files(
        workspace="w",
        files=[{"name": "guide.html", "data": b"<h1>Guide</h1><p>Use pnpm.</p>"}],
    )
    assert out["imported"] == 1 and out["errors"] == 0
    memories = svc.store.list_memories()
    assert memories[0].title == "Guide"
    assert memories[0].metadata["resource_kind"] == "document"
    assert memories[0].provenance["trusted"] is False


def test_large_resource_is_chunked_without_configuring_an_extractor():
    svc = MemoryService.create(":memory:", graph_extractor="none", extractor="none")
    text = ("# Long guide\n\nA durable paragraph about deployment.\n\n" * 4_000).encode()
    out = svc.import_files(
        workspace="w", files=[{"name": "large.md", "data": text}]
    )
    assert out["imported"] == 1
    assert len(svc.store.list_memories()) > 1
