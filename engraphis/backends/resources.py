"""Optional, local multi-format resource extraction.

The core memory engine only stores text. This backend converts local resources into
text plus bounded provenance while keeping heavy tools optional:

* text/code/Markdown/JSON/CSV/HTML: standard library
* DOCX: standard-library ZIP/XML extraction
* PDF: pypdf when installed
* images: Pillow + pytesseract when installed
* audio/video: faster-whisper when installed and explicitly configured

Nothing is uploaded. Missing optional tools produce actionable errors instead of
silently storing binary garbage.
"""
from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import tempfile
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

from engraphis.core.interfaces import ResourceDocument

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".json", ".jsonl", ".csv", ".tsv",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".xml", ".html", ".htm",
}
CODE_EXTENSIONS = {
    ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs",
    ".java", ".cs", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".hxx",
    ".sql", ".tf", ".tfvars", ".hcl", ".sh", ".ps1", ".rb", ".php", ".swift",
    ".kt", ".kts", ".scala", ".lua", ".r",
}
PDF_EXTENSIONS = {".pdf"}
DOCX_EXTENSIONS = {".docx"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".webp"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
SUPPORTED_EXTENSIONS = (
    TEXT_EXTENSIONS | CODE_EXTENSIONS | PDF_EXTENSIONS | DOCX_EXTENSIONS
    | IMAGE_EXTENSIONS | AUDIO_EXTENSIONS | VIDEO_EXTENSIONS
)
MAX_DOCX_XML_BYTES = 20_000_000
MAX_EXTRACTED_TEXT_CHARS = 200_000
MAX_IMAGE_PIXELS = 50_000_000
MAX_PDF_PAGES = 1_000
# Defense in depth: the service layer already caps import sizes, but this module is
# a reusable backend contract — bound raw input here too so a future direct caller
# can't feed an unbounded blob into full in-memory decode/extraction.
MAX_RESOURCE_BYTES = 100_000_000


class ResourceExtractionError(ValueError):
    """Safe, actionable extraction failure."""


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1
        elif tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(
            line.strip() for line in "".join(self.parts).splitlines() if line.strip()
        )


def supported_resource_extensions() -> set[str]:
    return set(SUPPORTED_EXTENSIONS)


def _media_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _base_metadata(name: str, data: bytes) -> dict:
    return {
        "resource_name": Path(name).name,
        "resource_extension": Path(name).suffix.lower(),
        "resource_bytes": len(data),
        "resource_sha256": hashlib.sha256(data).hexdigest(),
    }


def _title(text: str, fallback: str) -> str:
    for line in (text or "").splitlines():
        clean = line.strip().lstrip("#").strip()
        if clean:
            return clean[:300]
    return fallback[:300]


def _decode_text(data: bytes) -> str:
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _looks_binary(data: bytes) -> bool:
    sample = data[:8192]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        return True
    controls = sum(byte < 32 and byte not in (9, 10, 12, 13) for byte in sample)
    return controls / len(sample) > 0.02


def _docx_text(data: bytes) -> tuple[str, dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCX_XML_BYTES:
                raise ResourceExtractionError(
                    "DOCX document.xml is too large after decompression"
                )
            raw = archive.read(info)
    except (KeyError, zipfile.BadZipFile) as exc:
        raise ResourceExtractionError(f"invalid DOCX: {exc}") from exc
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", raw, flags=re.IGNORECASE):
        raise ResourceExtractionError("DOCX XML declarations and entities are not allowed")
    try:
        # The size cap and explicit DTD/entity rejection above make stdlib parsing
        # appropriate here without adding a hard XML dependency to the offline core.
        root = ElementTree.fromstring(raw)  # nosec B314
    except ElementTree.ParseError as exc:
        raise ResourceExtractionError(f"invalid DOCX XML: {exc}") from exc
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    paragraphs = []
    for paragraph in root.iter(namespace + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(namespace + "t")).strip()
        if text:
            paragraphs.append(text)
    return "\n\n".join(paragraphs), {"paragraphs": len(paragraphs)}


def _pdf_text(data: bytes) -> tuple[str, dict, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ResourceExtractionError(
            "PDF extraction needs pypdf: pip install \"engraphis[documents]\""
        ) from exc
    try:
        reader = PdfReader(io.BytesIO(data))
        total_pages = len(reader.pages)
        page_limit = min(total_pages, MAX_PDF_PAGES)
        pages = []
        empty = 0
        processed = 0
        text_chars = 0
        text_truncated = False
        for index in range(page_limit):
            page_text = (reader.pages[index].extract_text() or "").strip()
            processed += 1
            if not page_text:
                empty += 1
                continue
            separator_chars = 2 if pages else 0
            remaining = MAX_EXTRACTED_TEXT_CHARS - text_chars - separator_chars
            if remaining <= 0:
                text_truncated = True
                break
            if len(page_text) > remaining:
                page_text = page_text[:remaining]
                text_truncated = True
            pages.append(page_text)
            text_chars += separator_chars + len(page_text)
            if text_truncated:
                break
    except Exception as exc:
        raise ResourceExtractionError(f"PDF extraction failed: {exc}") from exc
    warnings = []
    if total_pages > MAX_PDF_PAGES:
        warnings.append(
            f"PDF page extraction limited to the first {MAX_PDF_PAGES} of "
            f"{total_pages} pages"
        )
    if empty:
        warnings.append(f"{empty} PDF page(s) had no extractable text; OCR may be needed")
    if text_truncated:
        warnings.append(
            f"extracted text truncated to {MAX_EXTRACTED_TEXT_CHARS} characters"
        )
    return "\n\n".join(pages), {
        "pages": total_pages,
        "pages_extracted": processed,
    }, warnings


def _image_text(data: bytes) -> tuple[str, dict]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError as exc:
        raise ResourceExtractionError(
            "Image OCR needs Pillow + pytesseract and the local Tesseract binary: "
            "pip install \"engraphis[documents]\""
        ) from exc
    try:
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_IMAGE_PIXELS:
            raise ResourceExtractionError(
                f"image is too large for OCR ({image.width}x{image.height})"
            )
        text = pytesseract.image_to_string(image)
        meta = {"width": image.width, "height": image.height, "format": image.format or ""}
    except Exception as exc:
        raise ResourceExtractionError(f"image OCR failed: {exc}") from exc
    return text.strip(), meta


def _transcribe_path(path: str) -> tuple[str, dict]:
    model_name = os.environ.get("ENGRAPHIS_WHISPER_MODEL", "").strip()
    if not model_name:
        raise ResourceExtractionError(
            "Audio/video transcription is opt-in. Install faster-whisper and set "
            "ENGRAPHIS_WHISPER_MODEL to a local model path or model name."
        )
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise ResourceExtractionError(
            "Audio/video transcription needs faster-whisper: "
            "pip install \"engraphis[transcription]\""
        ) from exc
    try:
        model = WhisperModel(
            model_name,
            device=os.environ.get("ENGRAPHIS_WHISPER_DEVICE", "auto"),
            compute_type=os.environ.get("ENGRAPHIS_WHISPER_COMPUTE_TYPE", "default"),
        )
        segments, info = model.transcribe(path, vad_filter=True)
        parts = [segment.text.strip() for segment in segments if segment.text.strip()]
    except Exception as exc:
        raise ResourceExtractionError(f"transcription failed: {exc}") from exc
    return "\n".join(parts), {
        "language": getattr(info, "language", ""),
        "language_probability": float(getattr(info, "language_probability", 0.0) or 0.0),
        "duration": float(getattr(info, "duration", 0.0) or 0.0),
    }


class LocalResourceExtractor:
    """Dependency-light local resource router."""

    def extract_bytes(self, name: str, data: bytes) -> ResourceDocument:
        if not isinstance(data, (bytes, bytearray)):
            raise ResourceExtractionError("resource data must be bytes")
        if len(data) > MAX_RESOURCE_BYTES:
            raise ResourceExtractionError(
                f"resource exceeds the {MAX_RESOURCE_BYTES}-byte extraction limit"
            )
        raw = bytes(data)
        suffix = Path(name).suffix.lower()
        fallback = Path(name).stem or "resource"
        media_type = _media_type(name)
        metadata = _base_metadata(name, raw)
        warnings: list[str] = []

        if suffix in PDF_EXTENSIONS:
            text, extra, warnings = _pdf_text(raw)
            kind = "pdf"
        elif suffix in DOCX_EXTENSIONS:
            text, extra = _docx_text(raw)
            kind = "document"
        elif suffix in IMAGE_EXTENSIONS:
            text, extra = _image_text(raw)
            kind = "image_ocr"
        elif suffix in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
                temp.write(raw)
                temp_path = temp.name
            try:
                text, extra = _transcribe_path(temp_path)
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            kind = "transcript"
        else:
            if suffix not in SUPPORTED_EXTENSIONS and _looks_binary(raw):
                raise ResourceExtractionError(
                    f"unsupported binary resource type: {suffix or '(no extension)'}"
                )
            text = _decode_text(raw)
            extra = {}
            if suffix in {".html", ".htm"}:
                parser = _TextHTMLParser()
                parser.feed(text)
                text = parser.text()
                kind = "document"
            elif suffix in CODE_EXTENSIONS:
                kind = "code"
            elif suffix in {".json", ".jsonl"}:
                kind = "data"
                if suffix == ".json":
                    try:
                        text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
                    except (json.JSONDecodeError, TypeError):
                        warnings.append("JSON could not be parsed; imported as plain text")
            else:
                kind = "document"
        metadata.update(extra)
        if not text.strip():
            raise ResourceExtractionError("resource produced no extractable text")
        if len(text) > MAX_EXTRACTED_TEXT_CHARS:
            warnings.append(
                f"extracted text truncated to {MAX_EXTRACTED_TEXT_CHARS} characters"
            )
            text = text[:MAX_EXTRACTED_TEXT_CHARS]
        return ResourceDocument(
            text=text.strip(),
            title=_title(text, fallback),
            kind=kind,
            media_type=media_type,
            metadata=metadata,
            warnings=warnings,
        )

    def extract_path(self, path: str) -> ResourceDocument:
        source = Path(path)
        if not source.exists():
            raise ResourceExtractionError(f"resource path not found: {path}")
        if not source.is_file():
            raise ResourceExtractionError(f"resource path is not a file: {path}")
        suffix = source.suffix.lower()
        if suffix in AUDIO_EXTENSIONS | VIDEO_EXTENSIONS:
            text, extra = _transcribe_path(str(source))
            stat = source.stat()
            digest = hashlib.sha256()
            with source.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            metadata = {
                "resource_name": source.name,
                "resource_extension": suffix,
                "resource_bytes": stat.st_size,
                "resource_sha256": digest.hexdigest(),
                **extra,
            }
            warnings = []
            if len(text) > MAX_EXTRACTED_TEXT_CHARS:
                warnings.append(
                    f"extracted text truncated to {MAX_EXTRACTED_TEXT_CHARS} characters"
                )
                text = text[:MAX_EXTRACTED_TEXT_CHARS]
            return ResourceDocument(
                text=text.strip(), title=_title(text, source.stem), kind="transcript",
                media_type=_media_type(source.name), metadata=metadata, warnings=warnings,
            )
        return self.extract_bytes(source.name, source.read_bytes())


def get_resource_extractor():
    return LocalResourceExtractor()
