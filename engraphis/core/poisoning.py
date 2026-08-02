"""Deterministic write-time guard for memory payloads.

This module intentionally does not attempt to decide whether a fact is true.  It
recognises a small, explainable set of prompt-injection and exfiltration shapes before
they receive a trust decision.  A match quarantines the payload for inspection instead
of dropping it, mutating trusted memories, or relying on an online classifier.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Any, Mapping, Optional


POLICY_VERSION = "deterministic-v3"
QUARANTINE_STATE = "quarantined"
REVIEW_PENDING = "pending"
REVIEW_APPROVED = "approved"

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
            r"(?:^|\s|;)\s*(?:system|developer|assistant)\s*"
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
    r"(?<!\w)(?:[a-z][ \t]+){2,}[a-z](?!\w)",
    re.IGNORECASE,
)

# Unicode TR39 v15.1.0 ASCII detector projection, vendored here from
# https://www.unicode.org/Public/security/15.1.0/confusables.txt.  The full TR39
# data maps many characters to non-ASCII and multi-character skeletons that cannot
# affect this ASCII-only signal grammar; this pinned projection retains every common
# Cyrillic/Greek ASCII-lookalike relevant to the detector without a runtime package.
# Keeping it in core also makes the policy deterministic and offline-capable.
TR39_CONFUSABLES_VERSION = "15.1.0"
_TR39_ASCII_SKELETON = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "с": "c", "е": "e", "һ": "h", "і": "i",
    "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "ѕ": "s", "т": "t", "у": "y", "х": "x", "ԁ": "d", "ԛ": "q",
    # Greek
    "α": "a", "β": "b", "ϲ": "c", "ε": "e", "η": "n", "ι": "i",
    "κ": "k", "μ": "m", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "γ": "y", "ζ": "z",
})

# Reconstruct only words that participate in the narrowly-scoped detector grammar.
# A generic whitespace-stripping regex cannot know where a run such as
# ``i g n o r e t h e i n s t r u c t i o n s`` should be separated and used to
# turn it into an unmatchable blob.  Exact segmentation keeps normal prose intact.
_SPACED_SIGNAL_WORDS = frozenset({
    "ignore", "disregard", "forget", "override", "bypass", "all", "any", "the",
    "previous", "instruction", "instructions", "rule", "rules", "prompt", "prompts",
    "system", "message", "messages", "reveal", "exfiltrate", "send", "upload",
    "export", "print", "display", "secret", "secrets", "credential", "credentials",
    "password", "passwords", "api", "key", "keys", "token", "tokens", "environment",
    "variable", "variables", "do", "not", "dont", "never", "tell", "inform", "mention",
    "show", "notify", "user", "owner", "operator", "when", "if", "later", "future",
    "next", "session", "agent", "request",
})


def _segment_signal_words(letters: str) -> tuple[str, ...]:
    """Return an exact signal-vocabulary segmentation, or no segmentation.

    The dynamic program is deliberately all-or-nothing: unknown text must retain its
    original spacing instead of being altered into a new phrase by a safety helper.
    """
    lower = letters.casefold()
    best: list[tuple[str, ...] | None] = [None] * (len(lower) + 1)
    best[0] = ()
    for end in range(1, len(lower) + 1):
        choices: list[tuple[str, ...]] = []
        for start in range(max(0, end - 16), end):
            word = lower[start:end]
            if word in _SPACED_SIGNAL_WORDS and best[start] is not None:
                choices.append((*best[start], word))
        if choices:
            # Prefer the fewest, then longest-leading, words for deterministic output.
            best[end] = min(choices, key=lambda words: (len(words), tuple(-len(w) for w in words)))
    return best[-1] or ()


def _restore_spaced_signal_words(match: re.Match[str]) -> str:
    letters = "".join(match.group(0).split())
    words = _segment_signal_words(letters)
    return " ".join(words) if words else match.group(0)


def _canonical_payload_text(text: str) -> str:
    """Normalize presentation tricks before deterministic signal checks.

    This is defense in depth, not an authority decision: public ingress remains pending
    review even if no current deterministic signal matches.
    """
    # Decompose after compatibility normalization so a precomposed accented glyph
    # cannot retain its mark merely because it is no longer category ``Mn``.
    normalized = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text or ""))
    parts: list[str] = []
    for character in normalized:
        category = unicodedata.category(character)
        if category in {"Cf", "Mn"}:
            continue
        if category == "Cc":
            # Controls must not survive into the detector text, but whitespace-like
            # controls still separate words. Replacing them before removal avoids
            # turning ``ignore\nprevious`` into an unmatchable single token.
            if character.isspace():
                parts.append(" ")
            continue
        parts.append(character)
    return _SINGLE_LETTER_RUN.sub(
        _restore_spaced_signal_words,
        unicodedata.normalize("NFKC", "".join(parts)).casefold().translate(_TR39_ASCII_SKELETON),
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


def provenance_is_approved(provenance: object) -> bool:
    """Require both explicit trust and an explicit human/local approval state."""
    return (
        provenance_is_trusted(provenance)
        and isinstance(provenance, Mapping)
        and provenance.get("review_state") == REVIEW_APPROVED
    )


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
        provenance_is_approved(provenance)
        and metadata_is_trusted(metadata)
        and inspection_eligible(provenance, metadata)
    )


def source_is_external(source: object) -> bool:
    """Recognize external producers, including namespaced adapter instances."""
    label = _canonical_payload_text(str(source or "")).strip().casefold()
    base = label.split(":", 1)[0].split("/", 1)[0]
    return base in EXTERNAL_SOURCES


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
