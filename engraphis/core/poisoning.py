"""Deterministic write-time guard for untrusted memory payloads.

This module intentionally does not attempt to decide whether a fact is true.  It
recognises a small, explainable set of prompt-injection and exfiltration shapes in
payloads that the caller has *already* labelled untrusted.  A match quarantines the
payload for inspection instead of dropping it, mutating trusted memories, or relying
on an online classifier.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping, Optional


POLICY_VERSION = "deterministic-v2"
QUARANTINE_STATE = "quarantined"

# Source labels below identify producers outside the local memory authority.  They
# are enforced by the service/sync boundaries, not trusted merely because a payload
# supplied a familiar-looking label next to ``trusted=true``.
EXTERNAL_SOURCES = frozenset({
    "api", "extractor", "import", "mcp", "postgres_introspector", "resource_extractor",
    "sync", "tool", "web",
})


@dataclass(frozen=True)
class PoisoningDecision:
    """A content-free policy result safe to persist in metadata and audit records."""

    quarantined: bool
    policy: str = POLICY_VERSION
    reasons: tuple[str, ...] = ()


# Each expression captures a behaviour that is unsafe in a memory payload on its
# own.  Keep these narrow and semantic: ordinary technical prose should not be
# quarantined merely for mentioning a shell, an API key, or a system prompt.
_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_override",
        re.compile(
            r"\b(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:all\s+|any\s+|the\s+|previous\s+){0,3}"
            r"(?:instructions?|rules?|prompts?|system\s+(?:messages?|prompts?))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "privilege_impersonation",
        re.compile(
            r"(?:^|\n)\s*(?:system|developer|assistant)\s*"
            r"(?:message|prompt|instructions?)\s*[:\-]",
            re.IGNORECASE,
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"\b(?:reveal|exfiltrate|send|upload|export|print|display)\b"
            r".{0,96}?\b(?:secrets?|credentials?|passwords?|api[ _-]?keys?|"
            r"tokens?|environment(?:\s+variables?)?|\.env)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "concealed_action",
        re.compile(
            r"\b(?:do\s+not|don't|never)\s+"
            r"(?:tell|inform|mention|show|notify)\s+(?:the\s+)?"
            r"(?:user|owner|operator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deferred_instruction",
        re.compile(
            r"\b(?:when|if)\s+(?:a\s+)?(?:later|future|next)\s+"
            r"(?:session|agent|request)\b.{0,160}?\b"
            r"(?:ignore|disregard|override)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "attack_canary_marker",
        re.compile(r"\batk_[a-z0-9_]*canary\b", re.IGNORECASE),
    ),
)

_SINGLE_LETTER_RUN = re.compile(
    r"(?<!\w)(?:[a-z]\s+){3,}[a-z](?!\w)",
    re.IGNORECASE,
)


def _canonical_payload_text(text: str) -> str:
    """Normalize common presentation tricks before deterministic signal checks."""
    normalized = unicodedata.normalize("NFKC", text or "")
    normalized = "".join(
        character for character in normalized
        if unicodedata.category(character) not in {"Cf", "Cc"} or character in "\n\t"
    )
    return _SINGLE_LETTER_RUN.sub(
        lambda match: "".join(match.group(0).split()),
        normalized,
    )


def detect_payload_signals(content: str, *, title: str = "") -> tuple[str, ...]:
    """Return content-free prompt-injection signal codes, independent of trust labels.

    Trust is an authority decision made by the caller. Detection is a separate safety
    signal so downstream grounded-answer code can apply defense in depth when content
    was accidentally or maliciously mislabeled as trusted.
    """
    haystack = _canonical_payload_text(f"{title}\n{content}")
    return tuple(sorted(code for code, pattern in _SIGNALS if pattern.search(haystack)))


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def provenance_is_trusted(provenance: object) -> bool:
    """Require explicit local approval before a record may enter prompt context.

    New local ``Store`` writes are stamped explicitly. Older rows without that stamp
    fail closed until the rescan/approval workflow has classified them.
    """
    return isinstance(provenance, Mapping) and provenance.get("trusted") is True


def metadata_is_trusted(metadata: object) -> bool:
    provenance = _mapping(metadata).get("provenance")
    return not isinstance(provenance, Mapping) or provenance_is_trusted(provenance)


def metadata_is_quarantined(metadata: object) -> bool:
    """Recognize either canonical quarantine marker without exposing raw metadata."""
    meta = _mapping(metadata)
    provenance = _mapping(meta.get("provenance"))
    quarantine = meta.get("quarantine")
    return bool(
        provenance.get("quarantined") is True
        or (isinstance(quarantine, Mapping) and quarantine.get("state") == QUARANTINE_STATE)
    )


def inspection_eligible(provenance: object, metadata: object = None) -> bool:
    """Whether a record may appear in non-model inspection/search results.

    Benign external memories remain useful evidence for raw recall and deterministic
    conflict resolution. Quarantined payloads are retained solely for explicit
    governance inspection and stay outside every normal retrieval arm.
    """
    dedicated = _mapping(provenance)
    return not (
        dedicated.get("quarantined") is True
        or metadata_is_quarantined(metadata)
    )


def prompt_eligible(provenance: object, metadata: object = None) -> bool:
    """Whether a record may enter agent/model context.

    Inspection visibility and prompt eligibility deliberately differ: an explicitly
    approved, non-quarantined record is required before anything is packed for an agent.
    """
    return (
        provenance_is_trusted(provenance)
        and metadata_is_trusted(metadata)
        and inspection_eligible(provenance, metadata)
    )


def source_is_external(source: object) -> bool:
    """Recognize external producers, including namespaced adapter instances."""
    label = str(source or "").strip().casefold()
    base = label.split(":", 1)[0].split("/", 1)[0]
    return base in EXTERNAL_SOURCES


def _is_explicitly_untrusted(provenance: Mapping[str, Any]) -> bool:
    """Only an explicit false label opts an input into payload inspection.

    Existing direct-core callers that omit provenance are trusted local writes.  This
    keeps their behaviour unchanged and ensures a string such as ``"false"`` cannot
    accidentally be interpreted as an authority-changing boolean.
    """
    return provenance.get("trusted") is False


def _is_sticky_quarantine(metadata: Mapping[str, Any]) -> bool:
    quarantine = metadata.get("quarantine")
    return isinstance(quarantine, Mapping) and quarantine.get("state") == QUARANTINE_STATE


def assess_untrusted_payload(content: str, *, title: str = "",
                             metadata: Optional[Mapping[str, Any]] = None) -> PoisoningDecision:
    """Return a deterministic quarantine decision for one proposed memory write.

    Quarantine is sticky through correction/promotion-derived metadata: an untrusted
    caller cannot make a quarantined record live simply by copying it into another
    write and claiming a different provenance.  Releasing content deliberately
    requires a fresh trusted write, not a metadata toggle.
    """
    meta = _mapping(metadata)
    if _is_sticky_quarantine(meta):
        return PoisoningDecision(True, reasons=("inherited_quarantine",))
    provenance = _mapping(meta.get("provenance"))
    if not _is_explicitly_untrusted(provenance):
        return PoisoningDecision(False)

    reasons = detect_payload_signals(content, title=title)
    return PoisoningDecision(bool(reasons), reasons=reasons)


def apply_quarantine_metadata(metadata: Mapping[str, Any],
                              decision: PoisoningDecision) -> dict[str, Any]:
    """Mark an already-detected payload without trusting caller-owned metadata.

    The policy writes the canonical values last.  In particular, neither an incoming
    ``trusted: true`` nor a forged ``quarantined: false`` can turn a detected payload
    into trusted/live content.  Reasons are codes rather than quoted payload text so
    audit and sync metadata remain safe to display.
    """
    if not decision.quarantined:
        return dict(metadata)
    out = dict(metadata)
    provenance = _mapping(out.get("provenance"))
    provenance.update({
        "trusted": False,
        "quarantined": True,
        "quarantine_policy": decision.policy,
        "quarantine_reasons": list(decision.reasons),
    })
    out["provenance"] = provenance
    out["quarantine"] = {
        "state": QUARANTINE_STATE,
        "policy": decision.policy,
        "reasons": list(decision.reasons),
    }
    return out
