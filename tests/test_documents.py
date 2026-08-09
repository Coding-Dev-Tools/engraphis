"""Focused coverage for the dependency-free universal document parser."""
from __future__ import annotations

import io
import zipfile
from typing import Union

import pytest

from engraphis.core.documents import (
    DOCUMENT_FORMATS,
    DocumentRecord,
    DocumentParseError,
    document_format_for_path,
    normalize_document_path,
    parse_document,
    scan_document_tree,
)


def _zip(parts: dict[str, Union[str, bytes]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in parts.items():
            archive.writestr(path, content)
    return output.getvalue()


def test_markdown_uses_obsidian_adapter_and_masks_code_discovery():
    record = parse_document(
        b"---\ntags: [project]\n---\n# Design\n[[Roadmap]] #active\n```md\n[[hidden]] #nope\n```\n",
        "notes/design.md",
    )
    assert record.format == "markdown"
    assert record.title == "Design"
    assert record.tags == ["project", "active"]
    assert [link.target for link in record.links] == ["Roadmap"]
    assert record.metadata["adapter"] == "obsidian-markdown"


@pytest.mark.parametrize(
    ("name", "raw", "expected"),
    [
        ("note.txt", b"# kept literal\nplain #tag", "text"),
        ("guide.rst", b"Guide\n=====\n\nA link https://example.test.", "rst"),
        ("page.html", b"<title>Page</title><p>Hello <b>world</b></p><script>bad()</script>", "html"),
        ("data.json", b'{"title":"Inventory", "items":[1,2]}', "json"),
        ("data.csv", b"name,role\nAda,Engineer\n", "csv"),
        ("data.tsv", b"name\trole\nAda\tEngineer\n", "tsv"),
    ],
)
def test_common_text_formats_preserve_readable_content(name, raw, expected):
    record = parse_document(raw, name)
    assert record.format == expected
    assert record.content
    assert record.body
    assert len(record.raw_sha256) == len(record.canonical_sha256) == 64
    if expected == "html":
        assert "Hello world" in record.body and "bad" not in record.body
        assert record.title == "Page"
    if expected == "csv":
        assert record.metadata["columns"] == ["name", "role"]
        assert record.metadata["rows"] == 1


def test_json_lines_are_parsed_as_independent_records():
    record = parse_document(
        b'{"id":1,"name":"one"}\n{"id":2,"name":"two"}\n',
        "records.jsonl",
    )
    assert record.metadata == {"json_kind": "jsonl", "records": 2}
    assert '"name": "two"' in record.body
    assert record.warnings == []


def test_stdlib_container_formats_extract_docx_odt_and_epub():
    docx = _zip({
        "word/document.xml": (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>Deployment plan</w:t></w:r></w:p></w:body></w:document>"
        ),
    })
    odt = _zip({
        "content.xml": (
            '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            "<office:body><office:text><text:p>ODT body</text:p></office:text></office:body>"
            "</office:document-content>"
        ),
    })
    epub = _zip({
        "META-INF/container.xml": (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="EPUB/book.opf"/></rootfiles></container>'
        ),
        "EPUB/book.opf": (
            '<package xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Book</dc:title></metadata>'
            '<manifest><item id="one" href="one.xhtml"/></manifest><spine><itemref idref="one"/>'
            "</spine></package>"
        ),
        "EPUB/one.xhtml": "<html><body><h1>Chapter</h1><p>EPUB body</p></body></html>",
    })
    assert parse_document(docx, "plan.docx").body == "Deployment plan"
    assert parse_document(odt, "plan.odt").body == "ODT body"
    epub_record = parse_document(epub, "book.epub")
    assert epub_record.title == "Book" and "EPUB body" in epub_record.body


def test_scan_is_safe_and_continues_after_per_file_errors(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "good.txt").write_text("good", encoding="utf-8")
    (tmp_path / "notes" / "bad.bin").write_bytes(b"\0\1")
    (tmp_path / "secret.txt").write_text("api_key: very-secret-value", encoding="utf-8")
    (tmp_path / ".hidden.txt").write_text("skip", encoding="utf-8")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (tmp_path / "linked.txt").symlink_to(outside)
    except (NotImplementedError, OSError):
        pass
    scan = scan_document_tree(tmp_path)
    assert [item.relative_path for item in scan.documents] == ["notes/good.txt"]
    assert ("notes/bad.bin", "unsupported document format") in [(x.relative_path, x.reason) for x in scan.skipped]
    assert {item.relative_path for item in scan.rejected} == {"secret.txt"}
    assert ".hidden.txt" in {item.relative_path for item in scan.skipped}


@pytest.mark.parametrize("path", ["../x.txt", "/x.txt", "C:/x.txt", "a/x.txt:stream", " x.txt", ""])
def test_paths_and_unsupported_or_dangerous_containers_fail_closed(path):
    with pytest.raises(DocumentParseError):
        normalize_document_path(path)
    with pytest.raises(DocumentParseError, match="unsupported"):
        parse_document(b"text", "unknown.xyz")
    with pytest.raises(DocumentParseError, match="binary"):
        parse_document(b"visible\x00hidden", "mislabelled.txt")
    archive = _zip({"../outside.xml": "oops", "word/document.xml": "<x/>"})
    with pytest.raises(DocumentParseError, match="unsafe member"):
        parse_document(archive, "unsafe.docx")


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("empty.txt", b" \t\r\n"),
        ("empty.rst", b"\n\n"),
        ("empty.html", b"<title>Metadata only</title><template>hidden</template>"),
        ("frontmatter.md", b"---\ntitle: Metadata only\ntags: [empty]\n---\n\n"),
    ],
)
def test_blank_documents_are_rejected_before_import_preview(name, raw):
    with pytest.raises(DocumentParseError, match="produced no readable text"):
        parse_document(raw, name)


@pytest.mark.parametrize(
    ("name", "raw", "expected_title"),
    [
        ("settings.yaml", b"title: YAML title\nitems:\n - one\n", "YAML title"),
        ("settings.toml", b'title = "TOML title"\n[build]\nvalue = 1\n', "TOML title"),
        ("settings.ini", b"[app]\nname = INI title\n", "settings"),
        ("config.xml", b'<config title="XML title"><item>readable</item><a href="https://example.test/a">link</a></config>', "XML title"),
        ("program.py", b"# https://example.test/should-not-link\n#tag\ndef run():\n    return 1\n", "program"),
    ],
)
def test_config_xml_and_source_formats_are_safe_and_readable(name, raw, expected_title):
    record = parse_document(raw, name)
    assert record.title == expected_title
    assert record.content
    if name.endswith(".xml"):
        assert "readable" in record.body
        assert [link.target for link in record.links] == ["https://example.test/a"]
    if name.endswith(".py"):
        assert record.tags == [] and record.links == [] and record.attachments == []


def test_rtf_and_additional_office_containers_are_dependency_free():
    rtf = parse_document(b"{\\rtf1\\ansi Hello\\par world}", "notes.rtf")
    assert "Hello" in rtf.body and "world" in rtf.body
    unicode_rtf = parse_document(
        b"{\\rtf1\\ansi\\uc1 Caf\\u233? {\\uc0\\u945} \\u233? Smile \\u-10179?\\u-8704?}",
        "unicode.rtf",
    )
    assert unicode_rtf.body == "Café α é Smile 😀"

    xlsx = _zip({
        "xl/sharedStrings.xml": "<sst><si><t>Revenue</t></si></sst>",
        "xl/worksheets/sheet1.xml": (
            "<worksheet><sheetData><row><c t=\"s\"><v>0</v></c><c><v>7</v></c>"
            "<c t=\"inlineStr\"><is><r><t>North</t></r>"
            "<r><t> America</t></r></is></c></row></sheetData></worksheet>"
        ),
    })
    pptx = _zip({
        "ppt/slides/slide1.xml": '<p:sld xmlns:p="urn:p" xmlns:a="urn:a"><a:t>Release deck</a:t></p:sld>',
    })
    ods = _zip({
        "content.xml": '<office:document-content xmlns:office="urn:o" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><text:p>ODS cell</text:p></office:document-content>',
    })
    odp = _zip({
        "content.xml": '<office:document-content xmlns:office="urn:o" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><text:h>ODP slide</text:h></office:document-content>',
    })
    assert "Revenue\t7\tNorth America" in parse_document(xlsx, "book.xlsx").body
    assert parse_document(pptx, "slides.pptx").body == "Release deck"
    assert parse_document(ods, "book.ods").body == "ODS cell"
    assert parse_document(odp, "slides.odp").body == "ODP slide"


def test_markup_and_markdown_code_cannot_create_discovery_records():
    html = parse_document(
        b'<title>Title only</title><h1>Visible heading</h1><a href="https://example.test/live">live</a><pre># hidden https://example.test/code</pre><template><a href="https://example.test/hidden">hidden</a><code># template-tag</code></template>',
        "page.html",
    )
    assert [link.target for link in html.links] == ["https://example.test/live"]
    assert html.headings == ["Visible heading"]
    assert "Title only" not in html.body
    assert "template-tag" not in html.body
    markdown = parse_document(
        b"```python\n[[Hidden]] [hidden](secret.md) #hidden\n````\n"
        b"[[Visible]] [Table](../metrics.csv) ![Diagram](images/plot.png) #visible\n"
        b"`[[also-hidden]] [hidden](private.md) #bad`\n",
        "note.md",
    )
    assert [link.target for link in markdown.links] == ["Visible", "../metrics.csv"]
    assert [item.path for item in markdown.attachments] == ["images/plot.png"]
    assert markdown.tags == ["visible"]


def test_xml_and_container_attacks_and_invalid_rtf_fail_closed():
    with pytest.raises(DocumentParseError, match="entities"):
        parse_document(b'<!DOCTYPE x [<!ENTITY boom "x">]><x>&boom;</x>', "unsafe.xml")
    with pytest.raises(DocumentParseError, match="invalid RTF"):
        parse_document(b"not rtf", "unsafe.rtf")
    encrypted = _zip({"word/document.xml": "<x/>"})
    payload = bytearray(encrypted)
    flags_offset = payload.find(b"PK\x01\x02") + 8
    if flags_offset >= 8:
        payload[flags_offset] |= 1
        with pytest.raises(DocumentParseError, match="encrypted"):
            parse_document(bytes(payload), "encrypted.docx")
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("word/document.xml", "<x/>")
        archive.writestr("word/document.xml", "<x/>")
    with pytest.raises(DocumentParseError, match="duplicate"):
        parse_document(duplicate.getvalue(), "duplicate.docx")


def test_adapter_contract_is_bounded_and_redacts_failures():
    raw = b"%PDF-local"
    with pytest.raises(DocumentParseError, match="requires an optional"):
        parse_document(raw, "report.pdf")

    def broken(*_args):
        raise RuntimeError("secret-local-path")

    with pytest.raises(DocumentParseError, match="adapter failed"):
        parse_document(raw, "report.pdf", adapter=broken)

    def adapter(data, path, mtime):
        text = "extracted document"
        return DocumentRecord(
            relative_path=path, format="pdf", media_type="application/pdf", title="Report",
            content=text, body=text, raw_sha256=__import__("hashlib").sha256(data).hexdigest(),
            canonical_sha256=__import__("hashlib").sha256(text.encode()).hexdigest(),
            source_size=len(data), source_mtime_ns=mtime,
        )

    assert parse_document(raw, "report.pdf", adapter=adapter).body == "extracted document"


def test_malformed_adapter_text_is_rejected_without_an_internal_exception():
    def adapter(data, path, mtime):
        return DocumentRecord(
            relative_path=path, format="pdf", media_type="application/pdf", title="Report",
            content=object(), body="text",  # type: ignore[arg-type]
            raw_sha256=__import__("hashlib").sha256(data).hexdigest(),
            canonical_sha256="not-reached", source_size=len(data), source_mtime_ns=mtime,
        )

    with pytest.raises(DocumentParseError, match="adapter returned invalid text"):
        parse_document(b"%PDF", "report.pdf", adapter=adapter)

    def invalid_unicode_adapter(data, path, mtime):
        content = "otherwise valid"
        return DocumentRecord(
            relative_path=path, format="pdf", media_type="application/pdf", title="Report",
            content=content, body="bad \ud800 body",
            raw_sha256=__import__("hashlib").sha256(data).hexdigest(),
            canonical_sha256=__import__("hashlib").sha256(content.encode()).hexdigest(),
            source_size=len(data), source_mtime_ns=mtime,
        )

    with pytest.raises(DocumentParseError, match="adapter returned invalid text"):
        parse_document(b"%PDF", "report.pdf", adapter=invalid_unicode_adapter)


def test_every_registered_extension_has_a_stable_dispatch_and_adapter_contract():
    for spec in DOCUMENT_FORMATS.values():
        for extension in spec.extensions:
            assert document_format_for_path("folder/document" + extension) == spec

    def adapter(data, path, mtime):
        spec = document_format_for_path(path)
        assert spec is not None
        text = spec.name + " text"
        import hashlib
        return DocumentRecord(
            relative_path=path, format=spec.name, media_type=spec.media_type,
            title=spec.name, content=text, body=text,
            raw_sha256=hashlib.sha256(data).hexdigest(),
            canonical_sha256=hashlib.sha256(text.encode()).hexdigest(),
            source_size=len(data), source_mtime_ns=mtime,
        )

    for spec in DOCUMENT_FORMATS.values():
        if spec.requires_adapter:
            record = parse_document(b"local-adapter-input", "item" + spec.extensions[0], adapter=adapter)
            assert record.format == spec.name


def test_text_and_container_bounds_apply_before_unbounded_materialization(monkeypatch):
    from engraphis.core import documents

    def should_not_parse(_value):
        raise AssertionError("oversized JSON reached the parser")

    monkeypatch.setattr(documents.json, "loads", should_not_parse)
    with pytest.raises(DocumentParseError, match="100000 character"):
        parse_document(b"{" + (b"x" * 100_000) + b"}", "huge.json")

    monkeypatch.setattr(documents, "parse_obsidian_note", should_not_parse)
    with pytest.raises(DocumentParseError, match="100000 character"):
        parse_document(b"#" + (b"x" * 100_000), "huge.md")

    monkeypatch.setattr(documents, "MAX_CONTAINER_TEXT_CHARS", 5)
    docx = _zip({
        "word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:t>sixsix</w:t></w:p></w:document>',
    })
    with pytest.raises(DocumentParseError, match="100000 character"):
        parse_document(docx, "huge.docx")

    xlsx = _zip({
        "xl/worksheets/sheet1.xml": (
            "<worksheet><sheetData><row><c t=\"inlineStr\"><is>"
            "<t>sixsix</t></is></c></row></sheetData></worksheet>"
        ),
    })
    with pytest.raises(DocumentParseError, match="100000 character"):
        parse_document(xlsx, "huge.xlsx")


def test_normalized_paths_are_bounded_and_portable(tmp_path):
    assert normalize_document_path("notes/cafe\u0301.txt") == "notes/café.txt"
    with pytest.raises(DocumentParseError, match="4096"):
        normalize_document_path(("a" * 4093) + ".txt")
    (tmp_path / "A.txt").write_text("one", encoding="utf-8")
    (tmp_path / "a.txt").write_text("two", encoding="utf-8")
    scan = scan_document_tree(tmp_path)
    assert len(scan.documents) == 1
    if len(list(tmp_path.iterdir())) == 2:
        assert any(item.reason == "duplicate normalized source path" for item in scan.rejected)
