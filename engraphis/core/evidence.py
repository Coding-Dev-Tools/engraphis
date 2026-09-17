"""Source-bound typed evidence used by opt-in agent integrations.

An exact value is useful only when it can be traced to the memory that supplied it.
This module deliberately does not infer values with an LLM or normalize the value
that a caller must copy.  It records the literal span in the authored source and
rejects ambiguous or mismatched bindings.
"""
from __future__ import annotations

from typing import Any, Optional


EXACT_VALUE_TYPES = frozenset({
    "literal", "string", "identifier", "path", "number", "date", "enum", "json",
})
MAX_EXACT_VALUE_CHARS = 4_096


def make_exact_value_binding(
    content: str,
    value: str,
    value_type: str = "literal",
    *,
    source_span: Optional[tuple[int, int]] = None,
) -> dict[str, Any]:
    """Return a validated, source-bound exact-value envelope.

    ``value`` is kept character-for-character as supplied.  A unique substring match is
    required unless an explicit ``source_span`` disambiguates repeated values.
    The returned offsets are Python string offsets into the original content,
    which keeps Unicode/code literals lossless for the local store.
    """
    if not isinstance(content, str) or not content:
        raise ValueError("exact value requires non-empty source content")
    if not isinstance(value, str) or not value:
        raise ValueError("exact_value must be a non-empty string")
    if len(value) > MAX_EXACT_VALUE_CHARS:
        raise ValueError("exact_value is too long")
    normalized_type = str(value_type or "literal").strip().casefold()
    if normalized_type not in EXACT_VALUE_TYPES:
        choices = ", ".join(sorted(EXACT_VALUE_TYPES))
        raise ValueError(f"exact_value_type must be one of: {choices}")

    if source_span is None:
        start = content.find(value)
        if start < 0:
            raise ValueError("exact_value must occur verbatim in source content")
        if content.find(value, start + 1) >= 0:
            raise ValueError("exact_value occurs more than once; provide exact_value_span")
        end = start + len(value)
    else:
        if (
            not isinstance(source_span, tuple)
            or len(source_span) != 2
            or any(isinstance(item, bool) or not isinstance(item, int) for item in source_span)
        ):
            raise ValueError("exact_value_span must be a (start, end) integer pair")
        start, end = source_span
        if start < 0 or end <= start or end > len(content) or content[start:end] != value:
            raise ValueError("exact_value_span does not match exact_value in source content")

    return {
        "value": value,
        "type": normalized_type,
        "source": "content",
        "start": start,
        "end": end,
        "copy_exactly": True,
    }


def exact_value_binding(
    metadata: object,
    *,
    content: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return a safe binding from memory metadata, or ``None`` when absent.

    When ``content`` is supplied, the recorded offsets are checked against the
    source text so hand-authored metadata cannot manufacture an exact literal.
    """
    if not isinstance(metadata, dict):
        return None
    binding = metadata.get("exact_value")
    if not isinstance(binding, dict):
        return None
    value = binding.get("value")
    value_type = binding.get("type")
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_EXACT_VALUE_CHARS
        or not isinstance(value_type, str)
        or value_type not in EXACT_VALUE_TYPES
        or binding.get("source") != "content"
        or binding.get("copy_exactly") is not True
    ):
        return None
    start = binding.get("start")
    end = binding.get("end")
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or start < 0
        or end <= start
    ):
        return None
    if content is not None and (end > len(content) or content[start:end] != value):
        return None
    return {
        "value": value,
        "type": value_type,
        "source": "content",
        "start": start,
        "end": end,
        "copy_exactly": True,
    }


def validate_exact_copy(binding: object, proposed: str) -> bool:
    """Check that a proposed literal contains the bound value unchanged."""
    if not isinstance(proposed, str):
        return False
    checked = exact_value_binding({"exact_value": binding})
    return bool(checked and checked["value"] in proposed)
