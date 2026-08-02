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
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private key", re.compile(r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----", re.I)),
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


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError, RecursionError):
        return str(value)


def _mapping_secret_kind(value: Any) -> str | None:
    """Catch environment/config mappings before JSON rendering obscures their keys."""
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            child_text = _text(child).strip().strip("\"'")
            if (_SENSITIVE_MAPPING_KEY.fullmatch(key_text) and len(child_text) >= 8
                    and not _REDACTION.fullmatch(child_text)):
                return "credential assignment"
            nested = _mapping_secret_kind(child)
            if nested:
                return nested
    elif isinstance(value, (list, tuple, set)):
        for child in value:
            nested = _mapping_secret_kind(child)
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


def reject_secrets(fields: Iterable[tuple[str, Any]]) -> None:
    """Reject the first secret found in persisted memory/event payload fields.

    Keep the public message content-free: callers log validation errors and can safely
    return this error through MCP/HTTP without accidentally re-emitting the secret.
    """
    for field, value in fields:
        kind = secret_kind(value)
        if kind:
            raise SecretDetectedError(field, kind)
