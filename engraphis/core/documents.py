"""Safe, dependency-free parsing and discovery for local documents.

This module describes source material only.  Persistence deliberately belongs to
the caller so previews, imports, and future source adapters can share one bounded
parser without coupling :mod:`engraphis.core` to the service or a database.

The parser is intentionally conservative: recognised formats are parsed through
the standard library; unknown and binary files are catalogued by a tree scan
instead of being decoded optimistically.  Markdown uses the existing Obsidian
parser as an adapter, preserving its frontmatter, wikilink, tag, and attachment
semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import codecs
import csv
import configparser
import hashlib
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union
import unicodedata
from urllib.parse import unquote, urlsplit
import zipfile
from xml.etree import ElementTree

from engraphis.core.obsidian import parse_obsidian_note
from engraphis.core.secrets import secret_kind


IMPORTER_VERSION = "1"
MAX_DOCUMENT_BYTES = 100_000_000
MAX_DOCUMENT_CHARS = 100_000
MAX_DOCUMENT_FILES = 10_000
MAX_DOCUMENT_TREE_BYTES = 250_000_000
MAX_CONTAINER_MEMBERS = 2_000
MAX_CONTAINER_XML_BYTES = 20_000_000
MAX_SOURCE_PATH_CHARS = 4_096
MAX_CONTAINER_TEXT_CHARS = MAX_DOCUMENT_CHARS
MAX_JSON_NESTING = 128
SENSITIVE_FILENAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "tokens", "tokens.json",
    "secrets", "secrets.json", "secret", "secret.json",
}


@dataclass(frozen=True)
class DocumentFormat:
    """One intentionally small, stdlib-readable document format declaration."""

    name: str
    extensions: Tuple[str, ...]
    media_type: str
    container: bool = False
    requires_adapter: bool = False


DOCUMENT_FORMATS: Dict[str, DocumentFormat] = {
    "markdown": DocumentFormat("markdown", (".md", ".markdown", ".mdown"), "text/markdown"),
    "text": DocumentFormat("text", (".txt", ".text", ".log"), "text/plain"),
    "rst": DocumentFormat("rst", (".rst", ".rest"), "text/x-rst"),
    "html": DocumentFormat("html", (".html", ".htm", ".xhtml"), "text/html"),
    "json": DocumentFormat("json", (".json", ".jsonl", ".ndjson"), "application/json"),
    "csv": DocumentFormat("csv", (".csv",), "text/csv"),
    "tsv": DocumentFormat("tsv", (".tsv", ".tab"), "text/tab-separated-values"),
    "docx": DocumentFormat("docx", (".docx",), "application/vnd.openxmlformats-officedocument.wordprocessingml.document", True),
    "odt": DocumentFormat("odt", (".odt",), "application/vnd.oasis.opendocument.text", True),
    "epub": DocumentFormat("epub", (".epub",), "application/epub+zip", True),
    "xlsx": DocumentFormat("xlsx", (".xlsx",), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", True),
    "pptx": DocumentFormat("pptx", (".pptx",), "application/vnd.openxmlformats-officedocument.presentationml.presentation", True),
    "ods": DocumentFormat("ods", (".ods",), "application/vnd.oasis.opendocument.spreadsheet", True),
    "odp": DocumentFormat("odp", (".odp",), "application/vnd.oasis.opendocument.presentation", True),
    "rtf": DocumentFormat("rtf", (".rtf",), "application/rtf"),
    "yaml": DocumentFormat("yaml", (".yaml", ".yml"), "application/yaml"),
    "toml": DocumentFormat("toml", (".toml",), "application/toml"),
    "ini": DocumentFormat("ini", (".ini", ".cfg"), "text/plain"),
    "xml": DocumentFormat("xml", (".xml",), "application/xml"),
    "source": DocumentFormat(
        "source",
        (
            ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
            ".go", ".rs", ".java", ".cs", ".c", ".h", ".cc", ".cpp",
            ".cxx", ".hpp", ".hh", ".hxx", ".sql", ".tf", ".tfvars",
            ".hcl", ".sh", ".ps1", ".rb", ".php", ".swift", ".kt", ".kts",
            ".scala", ".lua", ".r", ".css",
        ),
        "text/plain",
    ),
    "pdf": DocumentFormat("pdf", (".pdf",), "application/pdf", requires_adapter=True),
    "image": DocumentFormat(
        "image", (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"),
        "image/*", requires_adapter=True,
    ),
    "audio": DocumentFormat(
        "audio", (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"),
        "audio/*", requires_adapter=True,
    ),
    "video": DocumentFormat(
        "video", (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"),
        "video/*", requires_adapter=True,
    ),
}

DocumentAdapter = Callable[[bytes, str, Optional[int]], "DocumentRecord"]

_HEADING_RE = re.compile(r"(?m)^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_RST_OVERLINE_RE = re.compile(r"(?m)^(.+?)\n([=\-~^`:#*+]{3,})\s*$")
_TAG_RE = re.compile(r"(?<![\w/])#([\w\-/]+)")
_URL_RE = re.compile(r"(?<![\w@])https?://[^\s<>()\[\]{}]+", re.I)
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_MARKDOWN_ATTACHMENT_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_FENCE_START_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$")
_CONFIG_TITLE_RE = re.compile(r"(?mi)^\s*(?:title|name)\s*(?::|=)\s*[\"']?([^\n\"'#;]+)")


@dataclass(frozen=True)
class DocumentLink:
    target: str
    display_text: Optional[str] = None
    heading: Optional[str] = None
    block_id: Optional[str] = None
    embedded: bool = False


@dataclass(frozen=True)
class AttachmentReference:
    path: str
    embedded: bool = True


@dataclass
class DocumentRecord:
    """A parsed local document with original and readable representations."""

    relative_path: str
    format: str
    media_type: str
    title: str
    content: str
    body: str
    raw_sha256: str
    canonical_sha256: str
    source_size: int = 0
    source_mtime_ns: Optional[int] = None
    title_source: str = "filename"
    metadata: Dict[str, Any] = field(default_factory=dict)
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    dates: Dict[str, str] = field(default_factory=dict)
    headings: List[str] = field(default_factory=list)
    links: List[DocumentLink] = field(default_factory=list)
    attachments: List[AttachmentReference] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "relative_path": self.relative_path, "format": self.format,
            "media_type": self.media_type, "title": self.title,
            "content": self.content, "body": self.body,
            "raw_sha256": self.raw_sha256, "canonical_sha256": self.canonical_sha256,
            "source_size": self.source_size, "source_mtime_ns": self.source_mtime_ns,
            "title_source": self.title_source, "metadata": self.metadata,
            "frontmatter": self.frontmatter, "aliases": self.aliases,
            "tags": self.tags, "dates": self.dates, "headings": self.headings,
            "links": [link.__dict__ for link in self.links],
            "attachments": [item.__dict__ for item in self.attachments],
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class DocumentFileIssue:
    relative_path: str
    reason: str


@dataclass
class DocumentScan:
    root_path: str
    source_id: str
    documents: List[DocumentRecord] = field(default_factory=list)
    rejected: List[DocumentFileIssue] = field(default_factory=list)
    skipped: List[DocumentFileIssue] = field(default_factory=list)
    complete: bool = True

    @property
    def vault_path(self) -> str:
        """Compatibility-friendly synonym for source-oriented callers."""
        return self.root_path

    @property
    def notes(self) -> List[DocumentRecord]:
        """Read-only Obsidian-shaped view used by source import planners."""
        return self.documents

    @property
    def vault_id(self) -> str:
        """Read-only Obsidian-shaped identity alias for generic import planners."""
        return self.source_id


class DocumentParseError(ValueError):
    """Content-free parsing failure safe to surface in per-file reports."""


def supported_document_extensions() -> set[str]:
    return {extension for spec in DOCUMENT_FORMATS.values() for extension in spec.extensions}


def document_format_for_path(relative_path: str) -> Optional[DocumentFormat]:
    suffix = Path(relative_path).suffix.casefold()
    return next((spec for spec in DOCUMENT_FORMATS.values() if suffix in spec.extensions), None)


def canonical_source_id(root_path: Union[os.PathLike[str], str]) -> str:
    root = Path(root_path).resolve()
    return hashlib.sha256(str(root).encode("utf-8", "surrogatepass")).hexdigest()


def normalize_document_path(relative_path: str) -> str:
    """Return one safe POSIX source-relative path or reject traversal/absolute paths."""
    source = unicodedata.normalize("NFC", str(relative_path or ""))
    raw = source.replace("\\", "/")
    candidate = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw or source != source.strip() or candidate.is_absolute() or windows.is_absolute()
        or bool(windows.drive) or any(ord(char) < 32 for char in raw)
        or any(":" in part for part in candidate.parts)
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise DocumentParseError("source path must be a safe source-relative path")
    normalized = candidate.as_posix()
    if len(normalized) > MAX_SOURCE_PATH_CHARS:
        raise DocumentParseError("source path exceeds 4096 character safety limit")
    return normalized


def parse_document(
    raw: bytes, relative_path: str, *, source_mtime_ns: Optional[int] = None,
    adapter: Optional[DocumentAdapter] = None,
) -> DocumentRecord:
    """Parse one recognised document without executing its active content.

    ``content`` retains the canonical readable source representation.  ``body`` is
    the readable text for formats such as HTML and XML containers.  Discovery uses
    a code-masked representation so snippets cannot fabricate links or tags.
    """
    relative_path = normalize_document_path(relative_path)
    if not isinstance(raw, bytes):
        raise DocumentParseError("document data must be bytes")
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise DocumentParseError("document exceeds 100000000 byte safety limit")
    spec = document_format_for_path(relative_path)
    if spec is None:
        raise DocumentParseError("unsupported document format")
    fallback = Path(relative_path).stem or "document"
    if spec.requires_adapter:
        if adapter is None:
            raise DocumentParseError("document format requires an optional local adapter")
        try:
            record = adapter(raw, relative_path, source_mtime_ns)
        except (KeyboardInterrupt, SystemExit, DocumentParseError):
            raise
        except ValueError as exc:
            raise DocumentParseError(_safe_reason(exc)) from None
        except Exception:
            raise DocumentParseError("optional document adapter failed") from None
        if not isinstance(record, DocumentRecord):
            raise DocumentParseError("document adapter returned an invalid record")
        # Validate text before deriving its canonical hash.  Adapters are an
        # extension boundary; a malformed third-party adapter must produce the
        # same content-free per-file error as every other parser failure rather
        # than leaking an AttributeError/UnicodeEncodeError to a caller.
        if not isinstance(record.content, str) or not isinstance(record.body, str):
            raise DocumentParseError("document adapter returned invalid text")
        try:
            canonical_sha256 = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            # Validate both writable strings, not only the canonical content.
            # A lone surrogate in ``body`` otherwise escapes this boundary and
            # can fail later while serialising preview or memory metadata.
            record.body.encode("utf-8")
        except UnicodeEncodeError:
            raise DocumentParseError("document adapter returned invalid text") from None
        if (
            record.relative_path != relative_path or record.format != spec.name
            or record.source_size != len(raw)
            or record.raw_sha256 != hashlib.sha256(raw).hexdigest()
            or record.canonical_sha256 != canonical_sha256
            or record.source_mtime_ns != source_mtime_ns
        ):
            raise DocumentParseError("document adapter returned an invalid source identity")
        if not record.body.strip():
            raise DocumentParseError("document produced no readable text")
        if len(record.content) > MAX_DOCUMENT_CHARS or len(record.body) > MAX_DOCUMENT_CHARS:
            raise DocumentParseError("document exceeds 100000 character safety limit")
        if secret_kind(record.content) is not None or secret_kind(record.body) is not None:
            raise DocumentParseError("source appears to contain a secret")
        return record
    if not spec.container and _looks_binary(raw):
        raise DocumentParseError("binary content is not a readable document")
    if spec.name == "markdown":
        markdown_text, _warnings = _decode_text(raw)
        if len(markdown_text) > MAX_DOCUMENT_CHARS:
            raise DocumentParseError("document exceeds 100000 character safety limit")
        record = _markdown_record(raw, relative_path, source_mtime_ns)
        if not record.body.strip():
            raise DocumentParseError("document produced no readable text")
        return record
    if spec.name == "xml":
        body, title, metadata, warnings = _xml_body(raw, fallback)
        content = body
    elif spec.container:
        content, body, title, metadata, warnings = _parse_container(spec.name, raw)
    else:
        content, decode_warnings = _decode_rtf(raw) if spec.name == "rtf" else _decode_text(raw)
        content = _canonical(content)
        if len(content) > MAX_DOCUMENT_CHARS:
            raise DocumentParseError("document exceeds 100000 character safety limit")
        body, title, metadata, warnings = _parse_text_format(spec.name, content, relative_path)
        warnings = decode_warnings + warnings
    if len(content) > MAX_DOCUMENT_CHARS or len(body) > MAX_DOCUMENT_CHARS:
        raise DocumentParseError("document exceeds 100000 character safety limit")
    if not body.strip():
        raise DocumentParseError("document produced no readable text")
    if secret_kind(content) is not None or secret_kind(body) is not None:
        raise DocumentParseError("source appears to contain a secret")
    return _record(
        relative_path, spec, raw, content, body, title, metadata, warnings,
        source_mtime_ns=source_mtime_ns,
    )


def scan_document_tree(
    root_path: Union[os.PathLike[str], str], *, adapter: Optional[DocumentAdapter] = None,
) -> DocumentScan:
    """Discover recognised documents below a safe directory, continuing per-file errors."""
    selected = Path(root_path)
    try:
        selected_info = os.lstat(selected)
        if selected.is_symlink() or _is_reparse_point(selected_info):
            raise DocumentParseError("source root cannot be a symlink")
        root = selected.resolve(strict=True)
    except OSError as exc:
        raise DocumentParseError("source path must be an existing directory") from exc
    if not root.is_dir():
        raise DocumentParseError("source path must be an existing directory")
    result = DocumentScan(str(root), canonical_source_id(root))
    scanned_files = scanned_bytes = 0
    normalized_paths = set()
    portable_paths = set()
    for path, issue in _walk_tree(root, root):
        raw_relative = path.relative_to(root).as_posix()
        if issue:
            # The root itself is represented by "." when its directory listing fails.
            # Keep that sentinel as a valid issue path so the incomplete flag is set
            # before normal path validation can reject it.
            if raw_relative == ".":
                relative = "."
            else:
                try:
                    relative = normalize_document_path(raw_relative)
                except DocumentParseError as exc:
                    result.rejected.append(DocumentFileIssue(raw_relative, _safe_reason(exc)))
                    continue
            result.skipped.append(DocumentFileIssue(relative, issue))
            if issue in {"unreadable directory", "unreadable path"}:
                result.complete = False
            continue
        try:
            relative = normalize_document_path(raw_relative)
        except DocumentParseError as exc:
            result.rejected.append(DocumentFileIssue(raw_relative, _safe_reason(exc)))
            continue
        if relative in normalized_paths or relative.casefold() in portable_paths:
            result.rejected.append(DocumentFileIssue(relative, "duplicate normalized source path"))
            continue
        normalized_paths.add(relative)
        portable_paths.add(relative.casefold())
        scanned_files += 1
        if scanned_files > MAX_DOCUMENT_FILES:
            result.rejected.append(DocumentFileIssue(relative, "source exceeds 10000 file safety limit"))
            result.complete = False
            break
        if _sensitive_filename(path.name):
            result.rejected.append(DocumentFileIssue(relative, "sensitive filename"))
            continue
        if document_format_for_path(relative) is None:
            result.skipped.append(DocumentFileIssue(relative, "unsupported document format"))
            continue
        try:
            raw, mtime_ns = _read_tree_file(root, path)
            scanned_bytes += len(raw)
            if scanned_bytes > MAX_DOCUMENT_TREE_BYTES:
                result.rejected.append(DocumentFileIssue(relative, "source exceeds 250000000 byte safety limit"))
                result.complete = False
                break
            result.documents.append(parse_document(
                raw, relative, source_mtime_ns=mtime_ns, adapter=adapter,
            ))
        except (OSError, DocumentParseError, ValueError) as exc:
            result.rejected.append(DocumentFileIssue(relative, _safe_reason(exc)))
    return result


def _markdown_record(raw: bytes, relative_path: str, mtime_ns: Optional[int]) -> DocumentRecord:
    try:
        note = parse_obsidian_note(raw, relative_path, source_mtime_ns=mtime_ns)
    except ValueError as exc:
        if "no readable text" in str(exc):
            raise DocumentParseError("document produced no readable text") from None
        raise DocumentParseError(_safe_reason(exc)) from None
    # The compatibility parser masks ordinary fences. Reparse a locally masked copy
    # for discovery so unclosed or variable-length backtick fences cannot mint links,
    # tags, attachments, headings, or a title from code. The original body is retained.
    masked_body = _mask_code(note.body)
    discovered = note
    if masked_body != note.body:
        prefix = note.content[:-len(note.body)] if note.body else note.content
        try:
            discovered = parse_obsidian_note(
                (prefix + masked_body).encode("utf-8"), relative_path,
                source_mtime_ns=mtime_ns,
            )
        except ValueError as exc:
            raise DocumentParseError(_safe_reason(exc)) from None
    spec = DOCUMENT_FORMATS["markdown"]
    links = [
        DocumentLink(
            link.target, link.display_text, link.heading, link.block_id, link.embedded,
        )
        for link in discovered.links
    ]
    links = _dedupe_links([*links, *_links(masked_body)])
    attachments = [
        AttachmentReference(item.path, item.embedded) for item in discovered.attachments
    ]
    attachment_keys = {(item.path, item.embedded) for item in attachments}
    for item in _attachments(masked_body):
        key = (item.path, item.embedded)
        if key not in attachment_keys:
            attachment_keys.add(key)
            attachments.append(item)
    return DocumentRecord(
        relative_path=note.relative_path, format=spec.name, media_type=spec.media_type,
        title=discovered.title, content=note.content, body=note.body,
        raw_sha256=note.raw_sha256, canonical_sha256=note.canonical_sha256,
        source_size=note.source_size, source_mtime_ns=note.source_mtime_ns,
        title_source=discovered.title_source, metadata={"adapter": "obsidian-markdown"},
        frontmatter=discovered.frontmatter, aliases=discovered.aliases, tags=discovered.tags,
        dates=discovered.dates, headings=discovered.headings,
        links=links, attachments=attachments,
        warnings=note.warnings + [item for item in discovered.warnings if item not in note.warnings],
    )


def _record(
    relative_path: str, spec: DocumentFormat, raw: bytes, content: str, body: str,
    title: str, metadata: Dict[str, Any], warnings: List[str], *,
    source_mtime_ns: Optional[int],
) -> DocumentRecord:
    visible = "" if spec.name == "source" else _mask_code(body)
    headings = _headings(spec.name, visible)
    for heading in metadata.get("document_headings", []):
        if isinstance(heading, str) and heading.strip():
            headings.append(heading.strip()[:300])
    headings = _dedupe(headings)
    links = _links(visible)
    for target in metadata.get("document_links", []):
        if isinstance(target, str) and target:
            links.append(DocumentLink(target))
    links = _dedupe_links(links)
    attachments = _attachments(visible)
    tags = _dedupe(_TAG_RE.findall(visible))
    fallback = Path(relative_path).stem or "document"
    return DocumentRecord(
        relative_path=relative_path, format=spec.name, media_type=spec.media_type,
        title=title or (headings[0] if headings else fallback), content=content, body=body,
        raw_sha256=hashlib.sha256(raw).hexdigest(),
        canonical_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        source_size=len(raw), source_mtime_ns=source_mtime_ns,
        title_source="metadata" if title else "heading" if headings else "filename",
        metadata=metadata, tags=tags, headings=headings, links=links,
        attachments=attachments, warnings=warnings,
    )


def _decode_text(raw: bytes) -> Tuple[str, List[str]]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), []
        except UnicodeDecodeError:
            return raw.decode("utf-16", errors="replace"), ["invalid UTF-16 was replaced with U+FFFD"]
    try:
        return raw.decode("utf-8-sig"), []
    except UnicodeDecodeError:
        return raw.decode("utf-8-sig", errors="replace"), ["invalid UTF-8 was replaced with U+FFFD"]


_RTF_ANSI_CODE_PAGE_RE = re.compile(rb"\\ansicpg([0-9]+)")


def _decode_rtf(raw: bytes) -> Tuple[str, List[str]]:
    """Decode literal RTF bytes using the document's declared ANSI code page."""
    match = _RTF_ANSI_CODE_PAGE_RE.search(raw[:4096])
    encoding = "cp1252"
    if match:
        encoding = "cp%s" % match.group(1).decode("ascii")
        try:
            codecs.lookup(encoding)
        except LookupError:
            # _rtf_body() reports the malformed control word without exposing
            # the code-page value in the error surface.
            encoding = "cp1252"
    try:
        return raw.decode(encoding), []
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace"), [
            "invalid %s was replaced with U+FFFD" % encoding.upper(),
        ]


def _looks_binary(raw: bytes) -> bool:
    """Reject binary payloads even when an extension claims to be text."""
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    sample = raw[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    controls = sum(byte < 32 and byte not in (9, 10, 12, 13) for byte in sample)
    return controls / len(sample) > 0.02


def _is_reparse_point(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _parse_text_format(
    name: str, content: str, relative_path: str,
) -> Tuple[str, str, Dict[str, Any], List[str]]:
    fallback = Path(relative_path).stem or "document"
    if name == "html":
        parser = _ReadableHTMLParser()
        try:
            parser.feed(content)
            parser.close()
        except DocumentParseError:
            raise
        except Exception:
            raise DocumentParseError("invalid HTML document") from None
        metadata: Dict[str, Any] = {
            "html_title": parser.title,
            "document_headings": _dedupe(parser.headings),
            **parser.metadata,
        }
        return parser.text(), parser.title or fallback, metadata, []
    if name == "json":
        return _json_body(
            content, fallback,
            line_delimited=Path(relative_path).suffix.casefold() in {".jsonl", ".ndjson"},
        )
    if name in {"csv", "tsv"}:
        return _tabular_body(content, fallback, "\t" if name == "tsv" else None)
    if name in {"yaml", "toml"}:
        match = _CONFIG_TITLE_RE.search(content)
        title = match.group(1).strip()[:300] if match else fallback
        return content, title, {"config_kind": name}, []
    if name == "ini":
        return _ini_body(content, fallback)
    if name == "rtf":
        return _rtf_body(content, fallback)
    return content, fallback, {}, []


def _ini_body(content: str, fallback: str) -> Tuple[str, str, Dict[str, Any], List[str]]:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(content)
    except (configparser.Error, UnicodeError):
        return content, fallback, {"config_kind": "ini"}, ["INI could not be parsed; preserved as text"]
    title = parser.defaults().get("title") or parser.defaults().get("name") or fallback
    return content, str(title)[:300], {"config_kind": "ini", "sections": parser.sections()[:100]}, []


def _xml_body(raw: bytes, fallback: str) -> Tuple[str, str, Dict[str, Any], List[str]]:
    root = _xml_root(raw, "XML document")
    body = "\n".join(part.strip() for part in root.itertext() if part.strip())
    if not body:
        raise DocumentParseError("document produced no readable text")
    title = str(root.attrib.get("title") or root.attrib.get("name") or fallback)[:300]
    links = []
    for element in root.iter():
        for name in ("href", "src"):
            value = element.attrib.get(name)
            if value:
                links.append(value)
    return body, title, {"xml_root": root.tag, "document_links": _dedupe(links)}, []


def _rtf_body(content: str, fallback: str) -> Tuple[str, str, Dict[str, Any], List[str]]:
    if not content.lstrip().startswith("{\\rtf"):
        raise DocumentParseError("invalid RTF document")
    output: List[str] = []
    suppressed = [False]
    unicode_fallback = [1]
    ansi_code_page = ["cp1252"]
    pending_high_surrogate: Optional[int] = None
    destinations = {"colortbl", "datastore", "fonttbl", "info", "object", "pict", "stylesheet"}

    def append_text(value: str) -> None:
        nonlocal pending_high_surrogate
        if pending_high_surrogate is not None:
            output.append("\ufffd")
            pending_high_surrogate = None
        output.append(value)

    def append_unicode_unit(unit: int) -> None:
        nonlocal pending_high_surrogate
        if pending_high_surrogate is not None:
            if 0xDC00 <= unit <= 0xDFFF:
                output.append(chr(
                    0x10000
                    + ((pending_high_surrogate - 0xD800) << 10)
                    + (unit - 0xDC00)
                ))
                pending_high_surrogate = None
                return
            output.append("\ufffd")
            pending_high_surrogate = None
        if 0xD800 <= unit <= 0xDBFF:
            pending_high_surrogate = unit
        elif 0xDC00 <= unit <= 0xDFFF:
            output.append("\ufffd")
        else:
            output.append(chr(unit))

    index = 0
    while index < len(content):
        char = content[index]
        if char == "{":
            if len(suppressed) >= 256:
                raise DocumentParseError("RTF nesting exceeds safety limit")
            suppressed.append(suppressed[-1])
            unicode_fallback.append(unicode_fallback[-1])
            ansi_code_page.append(ansi_code_page[-1])
            index += 1
        elif char == "}":
            if len(suppressed) == 1:
                raise DocumentParseError("invalid RTF document")
            suppressed.pop()
            unicode_fallback.pop()
            ansi_code_page.pop()
            index += 1
        elif char != "\\":
            if not suppressed[-1]:
                append_text(char)
            index += 1
        elif index + 1 >= len(content):
            raise DocumentParseError("invalid RTF document")
        else:
            marker = content[index + 1]
            if marker == "*":
                suppressed[-1] = True
                index += 2
            elif marker == "'" and index + 3 < len(content):
                try:
                    decoded = bytes.fromhex(content[index + 2:index + 4]).decode(
                        ansi_code_page[-1],
                    )
                except (LookupError, UnicodeDecodeError, ValueError):
                    raise DocumentParseError("invalid RTF document") from None
                if not suppressed[-1]:
                    append_text(decoded)
                index += 4
            elif marker.isalpha():
                end = index + 1
                while end < len(content) and content[end].isalpha():
                    end += 1
                word = content[index + 1:end].casefold()
                number_start = end
                if word in destinations:
                    suppressed[-1] = True
                while end < len(content) and content[end] in "-0123456789":
                    end += 1
                number_text = content[number_start:end]
                try:
                    number = int(number_text) if number_text else None
                except ValueError:
                    number = None
                if end < len(content) and content[end] == " ":
                    end += 1
                if word == "uc" and number is not None:
                    unicode_fallback[-1] = max(0, min(number, MAX_DOCUMENT_CHARS))
                elif word == "ansicpg" and number is not None:
                    try:
                        code_page = f"cp{number}"
                        codecs.lookup(code_page)
                    except LookupError:
                        raise DocumentParseError("invalid RTF document") from None
                    ansi_code_page[-1] = code_page
                elif word == "u" and number is not None:
                    codepoint = number if number >= 0 else number + 0x10000
                    if 0 <= codepoint <= 0x10FFFF and not suppressed[-1]:
                        append_unicode_unit(codepoint)
                    remaining = unicode_fallback[-1]
                    while remaining and end < len(content):
                        # A fallback control symbol/word represents one character;
                        # never consume a group delimiter while skipping it.
                        if content[end] in "{}":
                            break
                        if content[end] == "\\":
                            end += 1
                            if end < len(content) and content[end].isalpha():
                                while end < len(content) and content[end].isalpha():
                                    end += 1
                                while end < len(content) and content[end] in "-0123456789":
                                    end += 1
                                if end < len(content) and content[end] == " ":
                                    end += 1
                        else:
                            end += 1
                        remaining -= 1
                    index = end
                    continue
                if not suppressed[-1] and word in {"line", "par"}:
                    append_text("\n")
                elif not suppressed[-1] and word == "tab":
                    append_text("\t")
                index = end
            else:
                if not suppressed[-1] and marker in {"~", "-", "_"}:
                    append_text(" " if marker != "_" else "-")
                elif not suppressed[-1] and marker in {"\\", "{", "}"}:
                    append_text(marker)
                index += 2
    if len(suppressed) != 1:
        raise DocumentParseError("invalid RTF document")
    if pending_high_surrogate is not None:
        output.append("\ufffd")
    body = _canonical("".join(output)).strip()
    if not body:
        raise DocumentParseError("document produced no readable text")
    return body, fallback, {"rtf": True}, []


def _json_body(
    content: str, fallback: str, *, line_delimited: bool = False,
) -> Tuple[str, str, Dict[str, Any], List[str]]:
    warnings: List[str] = []
    if _json_nesting_exceeds(content, MAX_JSON_NESTING):
        return content, fallback, {}, [
            "JSON nesting exceeds safety limit; preserved as text",
        ]
    try:
        if not line_delimited:
            value = json.loads(content)
            structured = _bounded_json_dump(value)
            if structured is None:
                return content, fallback, {}, [
                    "JSON output exceeds safety limit; preserved as text",
                ]
            title = str(value.get("title") or value.get("name") or fallback) if isinstance(value, dict) else fallback
            meta: Dict[str, Any] = {"json_kind": type(value).__name__}
            if isinstance(value, dict):
                meta["keys"] = sorted(str(key) for key in value)[:100]
            return structured, title, meta, warnings
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
        structured = _bounded_json_dump(rows)
        if structured is None:
            return content, fallback, {}, [
                "JSON output exceeds safety limit; preserved as text",
            ]
        return structured, fallback, {"json_kind": "jsonl", "records": len(rows)}, warnings
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        warnings.append("JSON could not be parsed; preserved as text")
        return content, fallback, {}, warnings


def _json_nesting_exceeds(content: str, limit: int) -> bool:
    """Bound JSON structure depth without parsing attacker-controlled objects."""
    depth = 0
    in_string = False
    escaped = False
    for char in content:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > limit:
                return True
        elif char in "]}":
            depth = max(0, depth - 1)
    return False


def _bounded_json_dump(value: Any) -> Optional[str]:
    """Pretty-print JSON without materializing output beyond the text budget."""
    encoder = json.JSONEncoder(
        ensure_ascii=False, indent=2, sort_keys=True,
    )
    chunks: List[str] = []
    length = 0
    try:
        for chunk in encoder.iterencode(value):
            length += len(chunk)
            if length > MAX_DOCUMENT_CHARS:
                return None
            chunks.append(chunk)
    except (RecursionError, TypeError, ValueError):
        return None
    return "".join(chunks)


def _tabular_body(content: str, fallback: str, delimiter: Optional[str]) -> Tuple[str, str, Dict[str, Any], List[str]]:
    try:
        dialect = csv.excel_tab if delimiter == "\t" else csv.Sniffer().sniff(content[:8192], delimiters=",;\t|")
        reader = csv.reader(io.StringIO(content), dialect=dialect)
        rows = list(reader)
    except (csv.Error, UnicodeError):
        return content, fallback, {}, ["table could not be parsed; preserved as text"]
    if not rows:
        return content, fallback, {"rows": 0, "columns": []}, []
    columns = rows[0][:200]
    return content, str(columns[0]).strip() or fallback, {"rows": len(rows) - 1, "columns": columns, "delimiter": dialect.delimiter}, []


def _parse_container(name: str, raw: bytes) -> Tuple[str, str, str, Dict[str, Any], List[str]]:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            _validate_archive(archive)
            if name == "docx":
                body, meta = _office_body(archive, "word/document.xml", "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}", ("p",), "t")
            elif name in {"odt", "ods", "odp"}:
                body, meta = _office_body(archive, "content.xml", "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}", ("h", "p"), None)
            elif name == "xlsx":
                body, meta = _xlsx_body(archive)
            elif name == "pptx":
                body, meta = _pptx_body(archive)
            else:
                body, meta = _epub_body(archive)
    except DocumentParseError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, KeyError):
        raise DocumentParseError("invalid %s archive" % name) from None
    content = body
    if not body.strip():
        raise DocumentParseError("document produced no readable text")
    return content, body, str(meta.get("title") or ""), meta, []


def _validate_archive(archive: zipfile.ZipFile) -> None:
    members = archive.infolist()
    if len(members) > MAX_CONTAINER_MEMBERS:
        raise DocumentParseError("container has too many members")
    total = 0
    names = set()
    for info in members:
        path = PurePosixPath(info.filename)
        if (
            path.is_absolute() or ".." in path.parts or not info.filename
            or "\\" in info.filename or "\x00" in info.filename
        ):
            raise DocumentParseError("container has unsafe member path")
        if info.filename in names:
            raise DocumentParseError("container has duplicate member paths")
        names.add(info.filename)
        if info.flag_bits & 0x1:
            raise DocumentParseError("encrypted containers are not supported")
        if info.is_dir():
            continue
        if info.file_size > MAX_CONTAINER_XML_BYTES:
            raise DocumentParseError("container member is too large after decompression")
        total += info.file_size
        if total > MAX_CONTAINER_XML_BYTES:
            raise DocumentParseError("container is too large after decompression")
        if info.compress_size and info.file_size > info.compress_size * 200:
            raise DocumentParseError("container compression ratio is unsafe")


def _xml_root(raw: bytes, label: str) -> ElementTree.Element:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, flags=re.I):
        raise DocumentParseError("%s XML declarations and entities are not allowed" % label)
    try:
        return ElementTree.fromstring(raw)  # nosec B314 -- explicit size and DTD limits above
    except ElementTree.ParseError:
        raise DocumentParseError("invalid %s XML" % label) from None


def _office_body(
    archive: zipfile.ZipFile, member: str, namespace: str, blocks: Tuple[str, ...], text_node: Optional[str],
) -> Tuple[str, Dict[str, Any]]:
    raw = archive.read(member)
    root = _xml_root(raw, "office document")
    values: List[str] = []
    total = 0
    block_tags = {namespace + block for block in blocks}
    for element in root.iter():
        if element.tag not in block_tags:
            continue
        nodes = element.itertext() if text_node is None else (node.text or "" for node in element.iter(namespace + text_node))
        text = _bounded_join(nodes, limit=MAX_CONTAINER_TEXT_CHARS - total).strip()
        if text:
            separator = 2 if values else 0
            if total + separator + len(text) > MAX_CONTAINER_TEXT_CHARS:
                raise DocumentParseError("document exceeds 100000 character safety limit")
            values.append(text)
            total += separator + len(text)
    return "\n\n".join(values), {"paragraphs": len(values)}


def _bounded_join(values: Iterable[str], *, limit: int) -> str:
    parts: List[str] = []
    total = 0
    for value in values:
        if not value:
            continue
        if total + len(value) > limit:
            raise DocumentParseError("document exceeds 100000 character safety limit")
        parts.append(value)
        total += len(value)
    return "".join(parts)


def _xlsx_body(archive: zipfile.ZipFile) -> Tuple[str, Dict[str, Any]]:
    shared: List[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        root = _xml_root(archive.read("xl/sharedStrings.xml"), "XLSX shared strings")
        shared = [_bounded_join(item.itertext(), limit=MAX_CONTAINER_TEXT_CHARS) for item in root if item.tag.endswith("si")]
    sheets = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)),
        key=lambda value: _archive_member_number(value),
    )
    rows: List[str] = []
    total = 0
    for name in sheets:
        root = _xml_root(archive.read(name), "XLSX worksheet")
        for row in (item for item in root.iter() if item.tag.endswith("row")):
            values: List[str] = []
            row_size = 0
            for cell in (item for item in row if item.tag.endswith("c")):
                cell_type = cell.attrib.get("t")
                if cell_type == "inlineStr":
                    inline = next(
                        (
                            node for node in cell
                            if node.tag == "is" or node.tag.endswith("}is")
                        ),
                        None,
                    )
                    value = (
                        _bounded_join(
                            inline.itertext(), limit=MAX_CONTAINER_TEXT_CHARS,
                        )
                        if inline is not None else ""
                    )
                else:
                    value = next(
                        (
                            node.text or "" for node in cell.iter()
                            if node.tag == "v" or node.tag.endswith("}v")
                        ),
                        "",
                    )
                if cell_type == "s" and value.isdigit() and int(value) < len(shared):
                    value = shared[int(value)]
                separator = 1 if values else 0
                if (
                    total + (1 if rows else 0) + row_size
                    + separator + len(value) > MAX_CONTAINER_TEXT_CHARS
                ):
                    raise DocumentParseError(
                        "document exceeds 100000 character safety limit"
                    )
                values.append(value)
                row_size += separator + len(value)
            text = "\t".join(values).strip()
            if text:
                if total + len(text) + (1 if rows else 0) > MAX_CONTAINER_TEXT_CHARS:
                    raise DocumentParseError("document exceeds 100000 character safety limit")
                rows.append(text)
                total += len(text) + (1 if rows else 0)
    return "\n".join(rows), {"sheets": len(sheets), "rows": len(rows)}


def _pptx_body(archive: zipfile.ZipFile) -> Tuple[str, Dict[str, Any]]:
    slides = sorted(
        (name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
        key=lambda value: _archive_member_number(value),
    )
    parts: List[str] = []
    total = 0
    for name in slides:
        root = _xml_root(archive.read(name), "PPTX slide")
        text = _bounded_join((item.text or "" for item in root.iter() if item.tag.endswith("}t")), limit=MAX_CONTAINER_TEXT_CHARS - total).strip()
        if text:
            if total + len(text) + (2 if parts else 0) > MAX_CONTAINER_TEXT_CHARS:
                raise DocumentParseError("document exceeds 100000 character safety limit")
            parts.append(text)
            total += len(text) + (2 if parts else 0)
    return "\n\n".join(parts), {"slides": len(slides)}


def _archive_member_number(value: str) -> int:
    match = re.search(r"\d+", Path(value).stem)
    return int(match.group(0)) if match else 0


_EPUB_XML_ENCODING_RE = re.compile(
    br"<\?xml\b[^>]*\bencoding\s*=\s*(['\"])([^'\"]+)\1",
    flags=re.I | re.S,
)


def _decode_epub_chapter(raw: bytes) -> str:
    """Decode one EPUB spine member before feeding it to the HTML parser."""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encoding = "utf-16"
    elif raw.startswith(b"<\x00"):
        # XML permits UTF-16 without a BOM; the first code unit identifies LE.
        encoding = "utf-16-le"
    elif raw.startswith(b"\x00<"):
        encoding = "utf-16-be"
    else:
        match = _EPUB_XML_ENCODING_RE.search(raw[:4096])
        if match:
            try:
                encoding = codecs.lookup(match.group(2).decode("ascii")).name
            except (LookupError, UnicodeError):
                raise DocumentParseError("invalid EPUB chapter encoding") from None
        else:
            encoding = "utf-8"
    return raw.decode(encoding, errors="replace")


def _epub_body(archive: zipfile.ZipFile) -> Tuple[str, Dict[str, Any]]:
    container = _xml_root(archive.read("META-INF/container.xml"), "EPUB container")
    rootfile = next((item.attrib.get("full-path", "") for item in container.iter() if item.tag.endswith("rootfile")), "")
    if not rootfile or rootfile.startswith("/") or ".." in PurePosixPath(rootfile).parts:
        raise DocumentParseError("invalid EPUB package path")
    package = _xml_root(archive.read(rootfile), "EPUB package")
    manifest = {item.attrib.get("id", ""): item.attrib.get("href", "") for item in package.iter() if item.tag.endswith("item")}
    spine = [item.attrib.get("idref", "") for item in package.iter() if item.tag.endswith("itemref")]
    base = PurePosixPath(rootfile).parent
    parts: List[str] = []
    for item_id in spine:
        href = manifest.get(item_id, "")
        parsed_href = urlsplit(href)
        href_path = unquote(parsed_href.path)
        candidate = base / href_path
        if (
            not href_path
            or parsed_href.scheme
            or parsed_href.netloc
            or candidate.is_absolute()
            or ".." in candidate.parts
        ):
            raise DocumentParseError("invalid EPUB content path")
        raw = archive.read(candidate.as_posix())
        text = _html_text(_decode_epub_chapter(raw), limit=MAX_CONTAINER_TEXT_CHARS - sum(len(part) for part in parts))
        if text:
            if sum(len(part) for part in parts) + len(text) + (2 if parts else 0) > MAX_CONTAINER_TEXT_CHARS:
                raise DocumentParseError("document exceeds 100000 character safety limit")
            parts.append(text)
    title = next(("".join(item.itertext()).strip() for item in package.iter() if item.tag.endswith("title") and "".join(item.itertext()).strip()), "")
    return "\n\n".join(parts), {"chapters": len(parts), "title": title}


class _ReadableHTMLParser(HTMLParser):
    def __init__(self, *, limit: Optional[int] = None) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.limit = limit
        self.size = 0
        self._ignored = 0
        self._code_depth = 0
        self._title = False
        self._heading_depth = 0
        self._heading_parts: List[str] = []
        self.headings: List[str] = []
        self.title = ""
        self.metadata: Dict[str, Any] = {}

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        lower = tag.casefold()
        if self._ignored:
            if lower in {"script", "style", "noscript", "template"}:
                self._ignored += 1
            return
        if lower in {"script", "style", "noscript", "template"}:
            self._ignored += 1
        elif lower == "title":
            self._title = True
        elif lower in {"pre", "code"}:
            if self._code_depth == 0:
                self._add("\n```\n")
            self._code_depth += 1
        elif lower in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            if self._heading_depth == 0:
                self._heading_parts = []
            self._heading_depth += 1
        elif lower == "meta":
            values = {str(key).casefold(): value for key, value in attrs}
            key, value = values.get("name") or values.get("property"), values.get("content")
            if key and value and key.casefold() in {"description", "author", "keywords"}:
                self.metadata[key.casefold()] = value[:1000]
        elif lower in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}:
            self._add("\n")
        if lower == "a":
            values = {str(key).casefold(): value for key, value in attrs}
            href = values.get("href")
            if href:
                self.metadata.setdefault("document_links", []).append(href)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.casefold()
        if self._ignored:
            if lower in {"script", "style", "noscript", "template"}:
                self._ignored -= 1
            return
        if lower in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading_depth:
            self._heading_depth -= 1
            if self._heading_depth == 0:
                heading = "".join(self._heading_parts).strip()
                if heading:
                    self.headings.append(heading[:300])
        if lower in {"script", "style", "noscript", "template"} and self._ignored:
            self._ignored -= 1
        elif lower == "title":
            self._title = False
        elif lower in {"pre", "code"} and self._code_depth:
            self._code_depth -= 1
            if self._code_depth == 0:
                self._add("\n```\n")
        elif lower in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "article", "section"}:
            self._add("\n")

    def handle_data(self, data: str) -> None:
        if self._title:
            self.title = (self.title + data).strip()[:300]
        if self._heading_depth:
            self._heading_parts.append(data)
        if not self._ignored and not self._title:
            self._add(data)

    def _add(self, value: str) -> None:
        if self.limit is not None and self.size + len(value) > self.limit:
            raise DocumentParseError("document exceeds 100000 character safety limit")
        self.parts.append(value)
        self.size += len(value)

    def text(self) -> str:
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def _html_text(content: str, *, limit: Optional[int] = None) -> str:
    parser = _ReadableHTMLParser(limit=limit)
    parser.feed(content)
    parser.close()
    return parser.text()


def _headings(format_name: str, visible: str) -> List[str]:
    if format_name == "rst":
        return _dedupe(match.group(1).strip() for match in _RST_OVERLINE_RE.finditer(visible))
    return [match.group(2).strip() for match in _HEADING_RE.finditer(visible)]


def _mask_code(text: str) -> str:
    lines = text.splitlines(keepends=True)
    masked: List[str] = []
    fence_char = ""
    fence_size = 0
    for line in lines:
        stripped = line.rstrip("\r\n")
        start = _FENCE_START_RE.match(stripped)
        if not fence_char and start:
            token = start.group(1)
            fence_char, fence_size = token[0], len(token)
            masked.append(_blank_code(line))
            continue
        if fence_char:
            if re.match(r"^[ \t]*%s{%d,}[ \t]*$" % (re.escape(fence_char), fence_size), stripped):
                fence_char, fence_size = "", 0
            masked.append(_blank_code(line))
            continue
        masked.append(_mask_inline_code(line))
    return "".join(masked)


def _blank_code(value: str) -> str:
    return "".join("\n" if char == "\n" else "\r" if char == "\r" else " " for char in value)


def _mask_inline_code(line: str) -> str:
    result = list(line)
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        end = index
        while end < len(line) and line[end] == "`":
            end += 1
        token = line[index:end]
        close = line.find(token, end)
        stop = close + len(token) if close >= 0 else len(line.rstrip("\r\n"))
        for cursor in range(index, stop):
            if result[cursor] not in "\r\n":
                result[cursor] = " "
        index = stop if stop > index else end
    return "".join(result)


def _links(text: str) -> List[DocumentLink]:
    links = [DocumentLink(match.group(1)) for match in _MARKDOWN_LINK_RE.finditer(text)]
    links.extend(DocumentLink(match.group(0).rstrip(".,;:")) for match in _URL_RE.finditer(text))
    return _dedupe_links(links)


def _attachments(text: str) -> List[AttachmentReference]:
    return [AttachmentReference(path) for path in _dedupe(match.group(1) for match in _MARKDOWN_ATTACHMENT_RE.finditer(text))]


def _dedupe_links(values: Iterable[DocumentLink]) -> List[DocumentLink]:
    result: List[DocumentLink] = []
    seen = set()
    for value in values:
        if value.target not in seen:
            seen.add(value.target)
            result.append(value)
    return result


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _canonical(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _sensitive_filename(name: str) -> bool:
    lowered = name.casefold()
    return (lowered in SENSITIVE_FILENAMES or lowered.startswith(".env.")
            or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
            or bool(re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", lowered)))


def _safe_reason(exc: BaseException) -> str:
    text = str(exc)
    if "optional local document extractor" in text:
        return "optional local extractor unavailable; install engraphis[documents]"
    if "local model path" in text:
        return "transcription requires an existing local model path"
    allowed = (
        "secret", "safety limit", "unsupported", "invalid", "unsafe", "unreadable", "binary",
        "too large", "no readable text", "changed during scan", "document data must be bytes",
    )
    return next((label for label in allowed if label in text), "document parsing failed")


def _read_tree_file(root: Path, path: Path) -> Tuple[bytes, int]:
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
        raise DocumentParseError("unsafe file type")
    if not _is_within(root, path.resolve(strict=True)):
        raise DocumentParseError("path escapes source root")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened) or not _same_identity(before, opened):
            raise DocumentParseError("file changed during scan")
        if opened.st_size > MAX_DOCUMENT_BYTES:
            raise DocumentParseError("document exceeds 100000000 byte safety limit")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_DOCUMENT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_DOCUMENT_BYTES:
                raise DocumentParseError("document exceeds 100000000 byte safety limit")
        finished, after = os.fstat(fd), os.lstat(path)
        if (not _same_identity(opened, finished) or opened.st_size != finished.st_size
                or opened.st_mtime_ns != finished.st_mtime_ns or stat.S_ISLNK(after.st_mode)
                or _is_reparse_point(after)
                or not _same_identity(finished, after) or not _is_within(root, path.resolve(strict=True))):
            raise DocumentParseError("file changed during scan")
        return b"".join(chunks), int(finished.st_mtime_ns)
    finally:
        os.close(fd)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    if left.st_dev or left.st_ino or right.st_dev or right.st_ino:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    return True


def _walk_tree(root: Path, directory: Path) -> Iterable[Tuple[Path, Optional[str]]]:
    try:
        entries = sorted(
            directory.iterdir(),
            key=lambda item: (
                unicodedata.normalize("NFC", item.name).casefold(),
                unicodedata.normalize("NFC", item.name),
            ),
        )
    except OSError:
        yield directory, "unreadable directory"
        return
    for entry in entries:
        try:
            relative = entry.relative_to(root)
            info = entry.lstat()
            if entry.is_symlink() or _is_reparse_point(info):
                yield entry, "symlink skipped"
            elif not _is_within(root, entry.resolve()):
                yield entry, "path escapes source root"
            elif any(part.startswith(".") for part in relative.parts):
                yield entry, "hidden/configuration path skipped"
            elif entry.is_dir():
                yield from _walk_tree(root, entry)
            elif entry.is_file():
                yield entry, None
        except OSError:
            yield entry, "unreadable path"


def _is_within(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "AttachmentReference", "DOCUMENT_FORMATS", "DocumentFileIssue", "DocumentFormat",
    "DocumentLink", "DocumentParseError", "DocumentRecord", "DocumentScan",
    "MAX_DOCUMENT_BYTES", "MAX_DOCUMENT_CHARS", "MAX_DOCUMENT_FILES", "MAX_DOCUMENT_TREE_BYTES",
    "canonical_source_id", "document_format_for_path", "normalize_document_path",
    "parse_document", "scan_document_tree", "supported_document_extensions",
]
