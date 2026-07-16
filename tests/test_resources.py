import io
import zipfile

import pytest

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
