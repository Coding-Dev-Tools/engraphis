import io
import sys
import types
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
