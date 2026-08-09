"""Offline, dependency-free parsing and safe discovery for Obsidian vaults.

This module intentionally only describes source material.  Persisting notes and
creating graph records belongs to the engine-level importer so that this core
utility remains usable for previews without opening a database.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
import unicodedata

from engraphis.core.secrets import secret_kind


IMPORTER_VERSION = "1"
MAX_NOTE_CHARS = 100_000
MAX_NOTE_BYTES = 2_000_000
MAX_VAULT_FILES = 10_000
MAX_VAULT_BYTES = 250_000_000
MAX_SOURCE_PATH_CHARS = 4_096
ATTACHMENT_SUFFIXES = {
    ".aac", ".avif", ".bmp", ".csv", ".epub", ".gif", ".jpeg", ".jpg",
    ".m4a", ".md", ".mkv", ".mov", ".mp3", ".mp4", ".ogg", ".pdf",
    ".png", ".svg", ".tif", ".tiff", ".wav", ".webm", ".webp",
}
SENSITIVE_FILENAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_dsa",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "tokens", "tokens.json",
    "secrets", "secrets.json", "secret", "secret.json",
}
_FENCE_START_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})[^\n]*$")
_WIKILINK_RE = re.compile(r"(!?)\[\[([^\]\r\n]+)\]\]")
_MARKDOWN_ATTACHMENT_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_H1_RE = re.compile(r"(?m)^\s*#\s+(.+?)\s*#*\s*$")
_HEADING_RE = re.compile(r"(?m)^\s*(#{1,6})\s+(.+?)\s*#*\s*$")
_TAG_RE = re.compile(r"(?<![\w/])#([\w\-/]+)")


@dataclass(frozen=True)
class ObsidianLink:
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
class ObsidianNote:
    relative_path: str
    title: str
    content: str
    body: str
    raw_sha256: str
    canonical_sha256: str
    source_size: int = 0
    source_mtime_ns: Optional[int] = None
    title_source: str = "filename"
    frontmatter: Dict[str, Any] = field(default_factory=dict)
    aliases: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    dates: Dict[str, str] = field(default_factory=dict)
    headings: List[str] = field(default_factory=list)
    links: List[ObsidianLink] = field(default_factory=list)
    attachments: List[AttachmentReference] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable preview record."""
        return {
            "relative_path": self.relative_path, "title": self.title,
            "content": self.content, "body": self.body,
            "raw_sha256": self.raw_sha256, "canonical_sha256": self.canonical_sha256,
            "source_size": self.source_size, "source_mtime_ns": self.source_mtime_ns,
            "title_source": self.title_source,
            "frontmatter": self.frontmatter, "aliases": self.aliases, "tags": self.tags,
            "dates": self.dates, "headings": self.headings,
            "links": [link.__dict__ for link in self.links],
            "attachments": [attachment.__dict__ for attachment in self.attachments],
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class ObsidianFileIssue:
    relative_path: str
    reason: str


@dataclass
class ObsidianVaultScan:
    vault_path: str
    vault_id: str
    notes: List[ObsidianNote] = field(default_factory=list)
    rejected: List[ObsidianFileIssue] = field(default_factory=list)
    skipped: List[ObsidianFileIssue] = field(default_factory=list)
    complete: bool = True


def canonical_vault_id(vault_path: Union[os.PathLike[str], str]) -> str:
    """Return a stable local identity without reading vault contents."""
    root = Path(vault_path).resolve()
    return hashlib.sha256(str(root).encode("utf-8", "surrogatepass")).hexdigest()


def normalize_obsidian_path(relative_path: str) -> str:
    """Return one safe POSIX vault-relative path or reject traversal/absolute input."""
    source = unicodedata.normalize("NFC", str(relative_path or ""))
    raw = source.replace("\\", "/")
    candidate = PurePosixPath(raw)
    windows = PureWindowsPath(raw)
    if (
        not raw
        or source != source.strip()
        or candidate.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(ord(character) < 32 for character in raw)
        or any(":" in part for part in candidate.parts)
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ValueError("source path must be a safe vault-relative path")
    normalized = candidate.as_posix()
    if len(normalized) > MAX_SOURCE_PATH_CHARS:
        raise ValueError("source path exceeds 4096 character safety limit")
    return normalized


def parse_obsidian_note(
    raw: bytes,
    relative_path: str,
    *,
    source_mtime_ns: Optional[int] = None,
) -> ObsidianNote:
    """Parse one Markdown file without evaluating any Obsidian/plugin content."""
    relative_path = normalize_obsidian_path(relative_path)
    if not isinstance(raw, bytes):
        raise ValueError("note data must be bytes")
    # Keep the direct parser as bounded as the filesystem and browser scanners.
    # Otherwise a caller that uses this public core API directly can force a very
    # large UTF-8 replacement decode before the character limit is reached.
    if len(raw) > MAX_NOTE_BYTES:
        raise ValueError("note exceeds 2000000 byte safety limit")
    warnings: List[str] = []
    raw_sha256 = hashlib.sha256(raw).hexdigest()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        content = raw.decode("utf-8-sig", errors="replace")
        warnings.append("invalid UTF-8 was replaced with U+FFFD")
    if len(content) > MAX_NOTE_CHARS:
        raise ValueError("note exceeds 100000 character safety limit")
    if _contains_secret(content):
        raise ValueError("source appears to contain a secret")
    canonical = content.replace("\r\n", "\n").replace("\r", "\n")
    frontmatter, body, fm_warnings = _split_frontmatter(canonical)
    warnings.extend(fm_warnings)
    if not body.strip():
        raise ValueError("note produced no readable text")
    visible = _mask_code(body)
    stem = Path(relative_path).stem
    h1 = _first_h1(visible)
    frontmatter_title = _text_value(frontmatter.get("title"))
    title = frontmatter_title or h1 or stem
    title_source = "frontmatter" if frontmatter_title else "heading" if h1 else "filename"
    aliases = _string_values(frontmatter.get("aliases"))
    tags = _dedupe(_normalise_tags(_string_values(frontmatter.get("tags"))) +
                   _normalise_tags(_TAG_RE.findall(visible)))
    dates = _date_values(frontmatter)
    headings = [match.group(2).strip() for match in _HEADING_RE.finditer(visible)]
    links = _links(visible)
    attachments = _attachments(visible, links)
    return ObsidianNote(
        relative_path=relative_path, title=title,
        content=canonical, body=body,
        raw_sha256=raw_sha256,
        canonical_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        source_size=len(raw), source_mtime_ns=source_mtime_ns,
        title_source=title_source,
        frontmatter=frontmatter, aliases=aliases, tags=tags, dates=dates,
        headings=headings, links=links, attachments=attachments, warnings=warnings,
    )


def scan_obsidian_vault(vault_path: Union[os.PathLike[str], str]) -> ObsidianVaultScan:
    """Recursively discover safe Markdown notes; symlinks and hidden trees are skipped."""
    selected_root = Path(vault_path)
    try:
        selected_info = os.lstat(selected_root)
        if selected_root.is_symlink() or _is_reparse_point(selected_info):
            raise ValueError("vault root cannot be a symlink")
        root = selected_root.resolve(strict=True)
    except OSError as exc:
        raise ValueError("vault path must be an existing directory") from exc
    if not root.is_dir():
        raise ValueError("vault path must be an existing directory")
    result = ObsidianVaultScan(vault_path=str(root), vault_id=canonical_vault_id(root))
    scanned_files = 0
    scanned_bytes = 0
    normalized_paths = set()
    portable_paths = set()
    for path, issue in _walk_vault(root, root):
        raw_relative = path.relative_to(root).as_posix()
        if issue:
            # The root itself is represented by "." when its directory listing
            # fails. Keep that sentinel as a valid issue path before normal path
            # validation can reject it, and defer reconciliation for all
            # unreadable paths so transient filesystem failures are not treated
            # as deletions.
            if raw_relative == ".":
                relative = "."
            else:
                try:
                    relative = normalize_obsidian_path(raw_relative)
                except ValueError as exc:
                    result.rejected.append(ObsidianFileIssue(raw_relative, str(exc)))
                    continue
            result.skipped.append(ObsidianFileIssue(relative, issue))
            if issue in {"unreadable directory", "unreadable path"}:
                result.complete = False
            continue
        try:
            relative = normalize_obsidian_path(raw_relative)
        except ValueError as exc:
            result.rejected.append(ObsidianFileIssue(raw_relative, str(exc)))
            continue
        if relative in normalized_paths or relative.casefold() in portable_paths:
            result.rejected.append(ObsidianFileIssue(relative, "duplicate normalized source path"))
            continue
        normalized_paths.add(relative)
        portable_paths.add(relative.casefold())
        if path.suffix.lower() != ".md":
            continue
        scanned_files += 1
        if scanned_files > MAX_VAULT_FILES:
            result.rejected.append(
                ObsidianFileIssue(relative, "vault exceeds 10000 Markdown file safety limit")
            )
            result.complete = False
            break
        if _sensitive_filename(path.name):
            result.rejected.append(ObsidianFileIssue(relative, "sensitive filename"))
            continue
        try:
            raw, source_mtime_ns = _read_vault_note(root, path)
            scanned_bytes += len(raw)
            if scanned_bytes > MAX_VAULT_BYTES:
                result.rejected.append(
                    ObsidianFileIssue(relative, "vault exceeds 250000000 byte safety limit")
                )
                result.complete = False
                break
            result.notes.append(
                parse_obsidian_note(
                    raw, relative, source_mtime_ns=source_mtime_ns,
                )
            )
        except OSError:
            result.rejected.append(ObsidianFileIssue(relative, "unreadable file"))
        except ValueError as exc:
            result.rejected.append(ObsidianFileIssue(relative, str(exc)))
    return result


def _read_vault_note(root: Path, path: Path) -> Tuple[bytes, int]:
    """Read one regular file without following a swapped symlink.

    ``O_NOFOLLOW`` closes the POSIX check/open race.  The lstat/fstat identity and
    post-read stability checks also fail closed on platforms where that flag is
    unavailable (notably Windows) and discard bytes if the directory entry changed.
    """
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before) or not stat.S_ISREG(before.st_mode):
        raise ValueError("unsafe file type")
    if not _is_within(root, path.resolve(strict=True)):
        raise ValueError("path escapes vault")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened) or not _same_file_identity(before, opened):
            raise ValueError("file changed during scan")
        if opened.st_size > MAX_NOTE_BYTES:
            raise ValueError("note exceeds 2000000 byte safety limit")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_NOTE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_NOTE_BYTES:
                raise ValueError("note exceeds 2000000 byte safety limit")
        finished = os.fstat(fd)
        after = os.lstat(path)
        if (
            not _same_file_identity(opened, finished)
            or opened.st_size != finished.st_size
            or opened.st_mtime_ns != finished.st_mtime_ns
            or stat.S_ISLNK(after.st_mode)
            or _is_reparse_point(after)
            or not _same_file_identity(finished, after)
            or not _is_within(root, path.resolve(strict=True))
        ):
            raise ValueError("file changed during scan")
        return b"".join(chunks), int(finished.st_mtime_ns)
    finally:
        os.close(fd)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare stable filesystem identity when the platform exposes it."""
    if left.st_dev or left.st_ino or right.st_dev or right.st_ino:
        return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)
    return True


def _is_reparse_point(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _walk_vault(root: Path, directory: Path) -> Iterable[Tuple[Path, Optional[str]]]:
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
                continue
            if not _is_within(root, entry.resolve()):
                yield entry, "path escapes vault"
                continue
            if any(part.startswith(".") for part in relative.parts):
                yield entry, "hidden/configuration path skipped"
                continue
            if entry.is_dir():
                yield from _walk_vault(root, entry)
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


def _split_frontmatter(content: str) -> Tuple[Dict[str, Any], str, List[str]]:
    if not (content.startswith("---\n") or content == "---"):
        return {}, content, []
    lines = content.splitlines(keepends=True)
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() in {"---", "..."}), None)
    if closing is None:
        return {}, content, ["unclosed YAML frontmatter treated as Markdown"]
    frontmatter, warnings = _parse_simple_yaml("".join(lines[1:closing]))
    return frontmatter, "".join(lines[closing + 1:]), warnings


def _parse_simple_yaml(source: str) -> Tuple[Dict[str, Any], List[str]]:
    values: Dict[str, Any] = {}
    warnings: List[str] = []
    active: Optional[str] = None
    for number, raw in enumerate(source.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        list_match = re.match(r"^\s+-\s+(.+?)\s*$", raw)
        if list_match and active:
            old = values.setdefault(active, [])
            if not isinstance(old, list):
                warnings.append("frontmatter line %d: mixed scalar/list ignored" % number)
            else:
                old.append(_yaml_scalar(list_match.group(1)))
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*?)\s*$", raw)
        if not match:
            warnings.append("frontmatter line %d: unsupported YAML construct" % number)
            active = None
            continue
        key, value = match.groups()
        active = key
        values[key] = ([] if not value else
                       _yaml_list(value) if value.startswith("[") and value.endswith("]") else _yaml_scalar(value))
    return values, warnings


def _yaml_list(value: str) -> List[str]:
    inside = value[1:-1].strip()
    if not inside:
        return []
    return [_yaml_scalar(part.strip()) for part in inside.split(",")]


def _yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


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
            if re.match(
                r"^[ \t]*%s{%d,}[ \t]*$"
                % (re.escape(fence_char), fence_size),
                stripped,
            ):
                fence_char, fence_size = "", 0
            masked.append(_blank_code(line))
            continue
        masked.append(_mask_inline_code(line))
    return "".join(masked)


def _blank_code(value: str) -> str:
    return "".join(
        "\n" if char == "\n" else "\r" if char == "\r" else " "
        for char in value
    )


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


def _links(text: str) -> List[ObsidianLink]:
    found: List[ObsidianLink] = []
    for match in _WIKILINK_RE.finditer(text):
        source = match.group(2).strip()
        target_part, separator, display = source.partition("|")
        target, heading, block_id = _split_link_target(target_part.strip())
        if target or heading or block_id:
            found.append(ObsidianLink(target, display.strip() or None if separator else None, heading, block_id, bool(match.group(1))))
    return found


def _split_link_target(value: str) -> Tuple[str, Optional[str], Optional[str]]:
    target, heading, block_id = value, None, None
    if "#" in target:
        target, heading = target.split("#", 1)
        heading = heading.strip() or None
    if "^" in target:
        target, block_id = target.split("^", 1)
        block_id = block_id.strip() or None
    elif heading and "^" in heading:
        heading, block_id = heading.split("^", 1)
        heading, block_id = heading.strip() or None, block_id.strip() or None
    return target.strip(), heading, block_id


def _attachments(text: str, links: List[ObsidianLink]) -> List[AttachmentReference]:
    paths = [link.target for link in links if link.embedded and _is_attachment(link.target)]
    paths.extend(match.group(1) for match in _MARKDOWN_ATTACHMENT_RE.finditer(text) if _is_attachment(match.group(1)))
    return [AttachmentReference(path) for path in _dedupe(paths)]


def _is_attachment(value: str) -> bool:
    return Path(value.split("#", 1)[0]).suffix.lower() in ATTACHMENT_SUFFIXES - {".md"}


def _first_h1(text: str) -> Optional[str]:
    match = _H1_RE.search(text)
    return match.group(1).strip() if match else None


def _string_values(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _normalise_tags(values: Iterable[str]) -> List[str]:
    return [value.strip().lstrip("#") for value in values if value.strip().lstrip("#")]


def _date_values(frontmatter: Dict[str, Any]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for key, value in frontmatter.items():
        if key.lower() in {"date", "created", "modified", "updated", "published"}:
            text = _text_value(value)
            if text:
                result[key] = text
    return result


def _text_value(value: Any) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _dedupe(values: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _sensitive_filename(name: str) -> bool:
    lowered = name.lower()
    return (lowered in SENSITIVE_FILENAMES or lowered.startswith(".env.") or
            lowered.endswith((".pem", ".key", ".p12", ".pfx")) or
            bool(re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", lowered)))


def _contains_secret(content: str) -> bool:
    return secret_kind(content) is not None
