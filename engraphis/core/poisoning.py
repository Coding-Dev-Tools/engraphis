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
# https://www.unicode.org/Public/security/15.1.0/confusables.txt.  It was generated
# from every single non-ASCII source code point in the pinned file whose target is an
# ASCII ``[a-z]+`` skeleton.  The full data maps many other characters to punctuation,
# combining marks, or non-ASCII skeletons that cannot affect this ASCII-only signal
# grammar.  Keeping the projection in core makes the policy deterministic and offline
# capable without taking a runtime dependency on a Unicode package.
TR39_CONFUSABLES_VERSION = "15.1.0"
# Format: ``ASCII skeleton`` followed by source code points in hex.  This generated
# direct projection contains 687 mappings.  The small explicit extension below keeps
# useful transitive Cyrillic/Greek lookalikes that TR39 represents through another
# non-ASCII skeleton rather than a direct ASCII target.
_TR39_ASCII_SKELETON_DATA = """\
a 237A FF41 1D41A 1D44E 1D482 1D4B6 1D4EA 1D51E 1D552 1D586
a 1D5BA 1D5EE 1D622 1D656 1D68A 251 3B1 1D6C2 1D6FC 1D736
a 1D770 1D7AA 430
aa A733
ae E6 4D5
ao A735
au A737
av A739 A73B
ay A73D
b 1D41B 1D44F 1D483 1D4B7 1D4EB 1D51F 1D553 1D587 1D5BB 1D5EF
b 1D623 1D657 1D68B 184 42C 13CF 1472 15AF
bl 42B
c FF43 217D 1D41C 1D450 1D484 1D4B8 1D4EC 1D520 1D554 1D588
c 1D5BC 1D5F0 1D624 1D658 1D68C 1D04 3F2 2CA5 441 ABAF
c 1043D
d 217E 2146 1D41D 1D451 1D485 1D4B9 1D4ED 1D521 1D555 1D589
d 1D5BD 1D5F1 1D625 1D659 1D68D 501 13E7 146F A4D2
dz 1F3 2A3
e 212E FF45 212F 2147 1D41E 1D452 1D486 1D4EE 1D522 1D556
e 1D58A 1D5BE 1D5F2 1D626 1D65A 1D68E AB32 435 4BD
f 1D41F 1D453 1D487 1D4BB 1D4EF 1D523 1D557 1D58B 1D5BF 1D5F3
f 1D627 1D65B 1D68F AB35 A799 17F 1E9D 584
ff FB00
ffi FB03
ffl FB04
fi FB01
fl FB02
g FF47 210A 1D420 1D454 1D488 1D4F0 1D524 1D558 1D58C 1D5C0
g 1D5F4 1D628 1D65C 1D690 261 1D83 18D 581
h FF48 210E 1D421 1D489 1D4BD 1D4F1 1D525 1D559 1D58D 1D5C1
h 1D5F5 1D629 1D65D 1D691 4BB 570 13C2
i 2DB 2373 FF49 2170 2139 2148 1D422 1D456 1D48A 1D4BE
i 1D4F2 1D526 1D55A 1D58E 1D5C2 1D5F6 1D62A 1D65E 1D692 131
i 1D6A4 26A 269 3B9 1FBE 37A 1D6CA 1D704 1D73E 1D778
i 1D7B2 456 A647 4CF AB75 13A5 118C3
ii 2171
iii 2172
ij 133
iv 2173
ix 2178
j FF4A 2149 1D423 1D457 1D48B 1D4BF 1D4F3 1D527 1D55B 1D58F
j 1D5C3 1D5F7 1D62B 1D65F 1D693 3F3 458
k 1D424 1D458 1D48C 1D4C0 1D4F4 1D528 1D55C 1D590 1D5C4 1D5F8
k 1D62C 1D660 1D694
l 5C0 2223 23FD FFE8 661 6F1 10320 1E8C7 1D7CF 1D7D9
l 1D7E3 1D7ED 1D7F7 1FBF1 FF29 2160 2110 2111 1D408 1D43C
l 1D470 1D4D8 1D540 1D574 1D5A8 1D5DC 1D610 1D644 1D678 196
l FF4C 217C 2113 1D425 1D459 1D48D 1D4C1 1D4F5 1D529 1D55D
l 1D591 1D5C5 1D5F9 1D62D 1D661 1D695 1C0 399 1D6B0 1D6EA
l 1D724 1D75E 1D798 2C92 406 4C0 5D5 5DF 627 1EE00
l 1EE80 FE8E FE8D 7CA 2D4F 16C1 A4F2 16F28 1028A 10309
lj 1C9
ll 2016 2225 2161 1C1 5F0
lll 2162
ls 2AA
lt 20B6
lz 2AB
n 1D427 1D45B 1D48F 1D4C3 1D4F7 1D52B 1D55F 1D593 1D5C7 1D5FB
n 1D62F 1D663 1D697 578 57C
nj 1CC
o C02 C82 D02 D82 966 A66 AE6 BE6 C66 CE6
o D66 E50 ED0 1040 665 6F5 FF4F 2134 1D428 1D45C
o 1D490 1D4F8 1D52C 1D560 1D594 1D5C8 1D5FC 1D630 1D664 1D698
o 1D0F 1D11 AB3D 3BF 1D6D0 1D70A 1D744 1D77E 1D7B8 3C3
o 1D6D4 1D70E 1D748 1D782 1D7BC 2C9F 43E 10FF 585 5E1
o 647 1EE24 1EE64 1EE84 FEEB FEEC FEEA FEE9 6BE FBAC
o FBAD FBAB FBAA 6C1 FBA8 FBA9 FBA7 FBA6 6D5 D20
o 101D 104EA 118C8 118D7 1042C
oe 153
oo 221E A74F A699
p 2374 FF50 1D429 1D45D 1D491 1D4C5 1D4F9 1D52D 1D561 1D595
p 1D5C9 1D5FD 1D631 1D665 1D699 3C1 3F1 1D6D2 1D6E0 1D70C
p 1D71A 1D746 1D754 1D780 1D78E 1D7BA 1D7C8 2CA3 440
q 1D42A 1D45E 1D492 1D4C6 1D4FA 1D52E 1D562 1D596 1D5CA 1D5FE
q 1D632 1D666 1D69A 51B 563 566
r 1D42B 1D45F 1D493 1D4C7 1D4FB 1D52F 1D563 1D597 1D5CB 1D5FF
r 1D633 1D667 1D69B AB47 AB48 1D26 2C85 433 AB81
rn 118E3 217F 1D426 1D45A 1D48E 1D4C2 1D4F6 1D52A 1D55E 1D592
rn 1D5C6 1D5FA 1D62E 1D662 1D696 11700
s FF53 1D42C 1D460 1D494 1D4C8 1D4FC 1D530 1D564 1D598 1D5CC
s 1D600 1D634 1D668 1D69C A731 1BD 455 ABAA 118C1 10448
sss 1F75C
st FB06
t 1D42D 1D461 1D495 1D4C9 1D4FD 1D531 1D565 1D599 1D5CD 1D601
t 1D635 1D669 1D69D
tf A777
ts 2A6
u 1D42E 1D462 1D496 1D4CA 1D4FE 1D532 1D566 1D59A 1D5CE 1D602
u 1D636 1D66A 1D69E A79F 1D1C AB4E AB52 28B 3C5 1D6D6
u 1D710 1D74A 1D784 1D7BE 57D 104F6 118D8
ue 1D6B
uo AB63
v 2228 22C1 FF56 2174 1D42F 1D463 1D497 1D4CB 1D4FF 1D533
v 1D567 1D59B 1D5CF 1D603 1D637 1D66B 1D69F 1D20 3BD 1D6CE
v 1D708 1D742 1D77C 1D7B6 475 5D8 11706 ABA9 118C0
vi 2175
vii 2176
viii 2177
w 26F 1D430 1D464 1D498 1D4CC 1D500 1D534 1D568 1D59C 1D5D0
w 1D604 1D638 1D66C 1D6A0 1D21 461 51D 561 1170A 1170E
w 1170F AB83
x 166E D7 292B 292C 2A2F FF58 2179 1D431 1D465 1D499
x 1D4CD 1D501 1D535 1D569 1D59D 1D5D1 1D605 1D639 1D66D 1D6A1
x 445 1541 157D
xi 217A
xii 217B
y 263 1D8C FF59 1D432 1D466 1D49A 1D4CE 1D502 1D536 1D56A
y 1D59E 1D5D2 1D606 1D63A 1D66E 1D6A2 28F 1EFF AB5A 3B3
y 213D 1D6C4 1D6FE 1D738 1D772 1D7AC 443 4AF 10E7 118DC
z 1D433 1D467 1D49B 1D4CF 1D503 1D537 1D56B 1D59F 1D5D3 1D607
z 1D63B 1D66F 1D6A3 1D22 AB93 118C4
"""

_TR39_TRANSITIVE_ASCII_OVERRIDES = {
    # Cyrillic
    "а": "a", "в": "b", "с": "c", "е": "e", "һ": "h", "і": "i",
    "ј": "j", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "ѕ": "s", "т": "t", "у": "y", "х": "x", "ԁ": "d", "ԛ": "q",
    # Greek
    "α": "a", "β": "b", "ϲ": "c", "ε": "e", "η": "n", "ι": "i",
    "κ": "k", "μ": "m", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "γ": "y", "ζ": "z",
}


def _build_tr39_ascii_skeleton() -> dict[int, str]:
    """Build the pinned detector translation table without external I/O."""
    projection: dict[str, str] = {}
    for row in _TR39_ASCII_SKELETON_DATA.splitlines():
        skeleton, *code_points = row.split()
        projection.update({chr(int(code_point, 16)): skeleton for code_point in code_points})
    projection.update(_TR39_TRANSITIVE_ASCII_OVERRIDES)
    return str.maketrans(projection)


_TR39_ASCII_SKELETON = _build_tr39_ascii_skeleton()

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


def _canonical_payload_text(text: str, *, format_as_space: bool = False) -> str:
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
        # UAX #44 has three combining-mark categories. Leaving either ``Mc`` or
        # ``Me`` behind lets an attacker split a detector keyword just as effectively
        # as a nonspacing mark (``Mn``), so canonical signal text strips all of them.
        if category == "Cf":
            # Source labels need the join-preserving form (``we<ZWSP>b`` -> ``web``),
            # while payload detection evaluates a second boundary-preserving form below
            # so ``ignore<ZWSP>previous`` cannot collapse into one unmatched token.
            if format_as_space:
                parts.append(" ")
            continue
        if category in {"Mn", "Mc", "Me"}:
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
    raw = f"{title}\n{content}"
    # Format controls serve both as invisible in-word glue and as invisible word
    # separators. Check both interpretations: a single global replacement strategy
    # necessarily leaves one of those two obfuscations undetected.
    haystacks = (
        _canonical_payload_text(raw),
        _canonical_payload_text(raw, format_as_space=True),
    )
    return tuple(sorted(
        code for code, pattern in _SIGNALS
        if any(pattern.search(haystack) for haystack in haystacks)
    ))


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
        # Quarantine always overrides approval: a detected payload must never keep
        # an approved review state (e.g. a local-agent write) that would hide it
        # from the review inbox.
        "review_state": QUARANTINE_STATE,
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
