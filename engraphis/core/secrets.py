"""Capture-time secret detection for local memory persistence.

This is intentionally a blocking boundary, not a sensitivity classifier.  A memory
database is searchable by its FTS/vector indexes while its process is running, so
labelling a credential ``secret`` after it is captured is not a protection.

The detector is deliberately conservative for credential-shaped values and never
includes the matched value in an exception, audit record, or response.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


class SecretDetectedError(ValueError):
    """A content-free rejection of an attempted credential write."""

    def __init__(self, field: str, kind: str) -> None:
        self.field = field
        self.kind = kind
        super().__init__(
            f"potential {kind} detected in {field}; redact it before storing memory"
        )


# Provider-specific prefixes and private-key/JWT forms have enough structure to be
# safe to block without a caller-supplied label.  Assignment detection below catches
# generic credentials (including private deployment tokens) only when the field name
# explicitly says that it is a credential.
_PEM_HEADER = "-----begin private key-----"
_PEM_HEADER_ALT = "-----begin "


def _contains_pem_header(value: str) -> bool:
    """O(N) PEM-header detection without regex backtracking.

    Equivalent to ``re.search(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.I)``
    but implemented as a case-normalized anchor scan: polynomial-ReDoS analysis
    of the optional ``[A-Z0-9]+`` group is moot when no regex engine is involved.
    Recognizes the canonical header and ``-----BEGIN X PRIVATE KEY-----`` variants
    (RSA/EC/OPENSSH/ENCRYPTED/…).
    """
    lowered = value.casefold()
    start = 0
    limit = len(lowered)
    header_exhausted = False
    header_pos = -1
    while start <= limit:
        # Walk both needles forward without ever rescanning earlier positions:
        # whichever of the two anchors occurs first is the only candidate that
        # can start a header there, so advance past it after inspection.
        # ``find`` results are monotone in ``start``: a miss is final for every
        # later start, and a hit at ``h`` is the answer for every start <= h.
        # Caching both avoids re-scanning to a far-away hit (or to end-of-string
        # on a miss) at every anchor — O(N) total instead of O(N*M).
        if not header_exhausted:
            if header_pos == -1 or start > header_pos:
                header_pos = lowered.find(_PEM_HEADER, start)
                if header_pos == -1:
                    header_exhausted = True
        header = header_pos if (header_pos != -1 and start <= header_pos) else -1
        anchor = lowered.find(_PEM_HEADER_ALT, start)
        if header != -1 and (anchor == -1 or header <= anchor):
            return True
        if anchor == -1:
            return False
        tail_start = anchor + len(_PEM_HEADER_ALT)
        if header == tail_start:
            return True
        tail = lowered[tail_start:tail_start + 40]
        for token in ("rsa ", "ec ", "dsa ", "openssh ", "encrypted ",
                      "pgp ", "pkcs8 "):
            if tail.startswith(token) and tail[len(token):].startswith(
                    "private key-----"):
                return True
        start = tail_start
    return False


class _PEMHeaderPattern:
    """Minimal stand-in for the removed PEM regex: a linear-time ``search`` and ``sub``."""

    @staticmethod
    def search(value: str) -> _PEMMatch | None:
        return _PEMMatch(True) if _contains_pem_header(value) else None

    @staticmethod
    def sub(repl: str, string: str, count: int = 0) -> str:
        return _redact_pem(string)


class _PEMMatch:
    """Truthiness-compatible stand-in for a regex match object."""

    __slots__ = ("_ok",)

    def __init__(self, ok: bool) -> None:
        self._ok = ok

    def __bool__(self) -> bool:
        return self._ok

    def group(self, *args: object) -> str:
        return _PEM_HEADER


_PATTERNS: tuple[tuple[str, Any], ...] = (
    ("private key", _PEMHeaderPattern()),
    ("AWS access key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox(?:a|b|p|r|s)-[A-Za-z0-9-]{10,}\b")),
    ("OpenAI API key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}\b", re.I)),
)

# Do not treat an explanatory phrase such as "password rotation" as a credential.
# A value must be assigned and be non-trivially long.  The negative look-ahead lets
# callers deliberately store the literal redaction marker in a fact or provenance
# field without disabling detection for real values.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:[a-z][a-z0-9]*[_-])*(?:
        api[_-]?key|(?:access|refresh|session|id)[_-]?token|token|auth(?:orization)?|
        bearer|password|passwd|client[_-]?secret|private[_-]?key|
        secret(?:[_-]?(?:access[_-]?key|key))?|database[_-]?(?:url|password)|
        connection[_-]?string
    )\b(?:[\"']\s*)?
    \s*(?:=|:)\s*[\"']?
    (?!<?(?:redacted|removed|withheld|not[_ -]?set)>?\b)
    [^\s\"']{8,}
    """
)
_DSN = re.compile(
    r"\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://"
    r"[^\s/@:]+:[^@\s]{8,}@",
    re.I,
)
_SENSITIVE_MAPPING_KEY = re.compile(
    r"""(?ix)
    (?:[a-z][a-z0-9]*[_\-.])*(?:
        api[_-]?key|(?:access|refresh|session|id)[_-]?token|token|auth(?:orization)?|
        bearer|password|passwd|client[_-]?secret|private[_-]?key|
        secret(?:[_-]?(?:access[_-]?key|key))?|database[_-]?(?:url|password)|
        connection[_-]?string
    )
    """,
)
_REDACTION = re.compile(r"^<?(?:redacted|removed|withheld|not[_ -]?set)>?$", re.I)
_REDACTED = "<redacted>"


def _redact_pem(text: str) -> str:
    """Safely redact PEM private key blocks in O(N) linear time without regex backtracking."""
    out: list[str] = []
    pos = 0
    upper = text.upper()
    while True:
        begin = upper.find("-----BEGIN", pos)
        if begin == -1:
            out.append(text[pos:])
            break
        header_end = upper.find("-----", begin + 10)
        if header_end == -1:
            out.append(text[pos:])
            break
        header = upper[begin:header_end + 5]
        if "PRIVATE KEY" not in header:
            out.append(text[pos:header_end + 5])
            pos = header_end + 5
            continue
        end = upper.find("-----END", header_end + 5)
        if end == -1:
            out.append(text[pos:])
            break
        footer_end = upper.find("-----", end + 8)
        if footer_end == -1:
            out.append(text[pos:])
            break
        footer = upper[end:footer_end + 5]
        if "PRIVATE KEY" not in footer:
            out.append(text[pos:footer_end + 5])
            pos = footer_end + 5
            continue
        out.append(text[pos:begin])
        out.append(_REDACTED)
        pos = footer_end + 5
    return "".join(out)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        try:
            return str(value)
        except Exception:
            return ""


def _mapping_secret_kind(
    value: Any,
    *,
    _seen: set[int] | None = None,
    _depth: int = 0,
) -> str | None:
    """Catch environment/config mappings before JSON rendering obscures their keys."""
    if not isinstance(value, (dict, list, tuple, set)):
        return None
    if _depth >= 64:
        return None
    seen = _seen if _seen is not None else set()
    marker = id(value)
    if marker in seen:
        return None
    seen.add(marker)
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_text = _text(child).strip().strip("\"'")
            if (_SENSITIVE_MAPPING_KEY.fullmatch(key_text) and len(child_text) >= 8
                    and not _REDACTION.fullmatch(child_text)):
                return "credential assignment"
            nested = _mapping_secret_kind(child, _seen=seen, _depth=_depth + 1)
            if nested:
                return nested
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            nested = _mapping_secret_kind(child, _seen=seen, _depth=_depth + 1)
            if nested:
                return nested
    return None


def secret_kind(value: Any) -> str | None:
    """Return a stable, non-sensitive category when *value* contains a secret."""
    mapped = _mapping_secret_kind(value)
    if mapped:
        return mapped
    value = _text(value)
    if not value:
        return None
    for kind, pattern in _PATTERNS:
        if pattern.search(value):
            return kind
    if _DSN.search(value):
        return "credential-bearing connection URI"
    if _ASSIGNMENT.search(value):
        return "credential assignment"
    return None


def redact_secrets(text: str) -> str:
    """Return *text* with credential-shaped values replaced by a safe marker.

    This is intentionally separate from :func:`reject_secrets`: product writes
    must still fail closed. It is for callers that explicitly need a safe copy
    of untrusted text, such as an evaluation corpus that must not persist an
    incidental credential found in source material. The returned text is
    suitable for the normal capture boundary and never includes the original
    matching value.
    """
    if not isinstance(text, str) or not text:
        return text
    safe = _redact_pem(text)
    # PEM blocks are handled by the linear-time ``_redact_pem`` above; the
    # detection entry in ``_PATTERNS`` is find-based and has no ``sub``.
    for _kind, pattern in _PATTERNS:
        if isinstance(pattern, _PEMHeaderPattern):
            continue
        safe = pattern.sub(_REDACTED, safe)
    safe = _DSN.sub(_REDACTED, safe)
    safe = _ASSIGNMENT.sub(_REDACTED, safe)
    return safe


def reject_secrets(fields: Iterable[tuple[str, Any]]) -> None:
    """Reject the first secret found in persisted memory/event payload fields.

    Keep the public message content-free: callers log validation errors and can safely
    return this error through MCP/HTTP without accidentally re-emitting the secret.
    """
    for field, value in fields:
        kind = secret_kind(value)
        if kind:
            raise SecretDetectedError(field, kind)
