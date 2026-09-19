"""Source-bound typed evidence used by opt-in agent integrations.

An exact value is useful only when it can be traced to the memory that supplied it.
This module deliberately does not infer values with an LLM or normalize the value
that a caller must copy.  It records the literal span in the authored source and
rejects ambiguous or mismatched bindings.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, Optional


EXACT_VALUE_TYPES = frozenset({
    "literal", "string", "identifier", "path", "number", "date", "enum", "json",
})
MAX_EXACT_VALUE_CHARS = 4_096
MAX_ACTION_FIELD_CHARS = 256
MAX_SOURCE_ID_CHARS = 512


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
        or end - start != len(value)
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


def _valid_action_field(field: object) -> bool:
    return bool(
        isinstance(field, str) and field and field == field.strip()
        and len(field) <= MAX_ACTION_FIELD_CHARS
        and not any(character in field for character in "\r\n\x00")
        and all(part and part == part.strip() for part in field.split("."))
    )


def _valid_source_id(source_id: object) -> bool:
    return bool(
        isinstance(source_id, str) and source_id and source_id == source_id.strip()
        and len(source_id) <= MAX_SOURCE_ID_CHARS
        and not any(character in source_id for character in "\r\n\x00")
    )


def make_action_contract(
    *,
    destination_field: str,
    source_id: str,
    binding: object,
    authorized: bool = False,
) -> dict[str, Any]:
    """Build an opt-in, source-bound contract for a file or tool action.

    The contract carries only the literal and its provenance.  It does not execute
    an action or grant authorization; callers must explicitly set ``authorized``
    after applying their host's authorization decision.
    """
    field = destination_field.strip() if isinstance(destination_field, str) else ""
    if not _valid_action_field(field):
        raise ValueError("destination_field must be a bounded single-line name")
    owner = source_id.strip() if isinstance(source_id, str) else ""
    if not _valid_source_id(owner):
        raise ValueError("source_id must be a bounded non-empty identifier")
    checked = exact_value_binding({"exact_value": binding})
    if checked is None:
        raise ValueError("action contract requires a validated source-bound exact value")
    return {
        "schema": "engraphis-action-contract/v1",
        "destination_field": field,
        "source_id": owner,
        "source_span": [checked["start"], checked["end"]],
        "value": checked["value"],
        "value_type": checked["type"],
        "authorized": authorized is True,
        "copy_exactly": True,
    }


def _action_value(proposed: object, field: str) -> tuple[bool, object]:
    """Read a destination value from a mapping, supporting bounded dotted paths."""
    if not isinstance(proposed, Mapping):
        return False, None
    current: object = proposed
    for part in field.split("."):
        if not part or not isinstance(current, Mapping) or part not in current:
            return False, None
        current = current[part]
    return True, current


def validate_action_contract(
    contract: object,
    proposed: object,
    *,
    authorized: Optional[bool] = None,
) -> dict[str, Any]:
    """Validate a proposed file/tool result against an action contract.

    This returns structured diagnostics rather than raising so a benchmark can score
    authorization, schema validity and literal preservation independently. A string
    proposal must be a JSON object with the same destination-field contract as a
    mapping. No normalization or case folding is performed on literals.
    """
    result = {
        "valid": False,
        "authorized": False,
        "literal_preserved": False,
        "source_id_match": False,
        "reason": "invalid_contract",
    }
    if not isinstance(contract, Mapping) or contract.get("schema") != "engraphis-action-contract/v1":
        return result
    field = contract.get("destination_field")
    source_id = contract.get("source_id")
    value = contract.get("value")
    value_type = contract.get("value_type", "literal")
    span = contract.get("source_span")
    if (
        not isinstance(field, str) or not _valid_action_field(field)
        or not _valid_source_id(source_id)
        or not isinstance(value, str) or not value
        or not isinstance(span, list) or len(span) != 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in span)
    ):
        return result
    checked = exact_value_binding({
        "exact_value": {
            "value": value, "type": value_type, "source": "content",
            "start": span[0], "end": span[1], "copy_exactly": True,
        }
    })
    if checked is None or contract.get("copy_exactly") is not True:
        result["reason"] = "unbound_literal"
        return result
    is_authorized = contract.get("authorized") is True
    if authorized is not None:
        is_authorized = is_authorized and authorized is True
    result["authorized"] = is_authorized
    if not is_authorized:
        result["reason"] = "unauthorized"
        return result

    if isinstance(proposed, str):
        try:
            proposed = json.loads(proposed)
        except (ValueError, RecursionError):
            result["reason"] = "invalid_proposal"
            return result
    if not isinstance(proposed, Mapping):
        result["reason"] = "invalid_proposal"
        return result
    present, candidate = _action_value(proposed, field)
    source_match = "source_id" not in proposed or proposed.get("source_id") == source_id
    if present:
        present = isinstance(candidate, str) and candidate == value
    result["literal_preserved"] = bool(present)
    result["source_id_match"] = bool(source_match)
    if not source_match:
        result["reason"] = "source_mismatch"
    elif not present:
        result["reason"] = "literal_changed_or_missing"
    else:
        result["valid"] = True
        result["reason"] = "accepted"
    return result
