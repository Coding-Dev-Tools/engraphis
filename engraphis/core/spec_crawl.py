"""Deterministic spec / prompt crawler ("Spec Crawl").

A spec crawl reads an agent instruction document (a prompt, ``AGENTS.md``, a skill file,
or a set of procedural memories) section by section, classifies every word into a small,
explainable set of kinds, links the meaningful ones, and turns unbounded wording into
**questions to ask the human** ("ask, don't guess"). Claims can optionally be traced to
existing memories through an injected ``support_lookup`` callable.

The module is deliberately pure and dependency-free: no backend, no network, no LLM, no
NumPy. The output is a canonical, replayable ``trace`` (ordered ``walk``/``read``/``link``
/``flag``/``trace`` events) plus a bounded report. Any animation is only a replay of that
trace, so identical input always yields an identical result and the dashboard needs no
streaming transport. Spec text is untrusted data: it is never executed or interpreted as
instructions; instruction-like payloads are surfaced as ``injection`` flags.
"""
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from engraphis.core.poisoning import detect_payload_signals
from engraphis.core.textutil import jaccard, tokenize

ALGORITHM_VERSION = "spec-crawl-v1"
MAX_SPEC_CHARS = 64_000
MAX_SPEC_WORDS = 5_000
MAX_SECTIONS = 64
MAX_TRACED_CLAIMS = 40
MAX_FLAGS = 200
MAX_NODE_CONFLICTS = 50
MAX_CONFLICT_REMEDIES = 10
MAX_MEMORY_SIMILARITY_TOKENS = 512
VAGUE_QUALIFIER_WINDOW = 8
LEGS = 16

#: Token kinds, in display order. ``word`` is read but neither linked nor flagged.
TOKEN_KINDS = (
    "owner", "approval", "spec", "source", "claim", "action", "vague", "word",
)
LINKED_KINDS = frozenset({"owner", "approval", "spec", "source", "claim", "action"})
FLAG_KINDS = ("vague", "untraced", "injection")

#: Default template axes (the seven-axis coverage radar) and their heading aliases.
#: Matching is by exact heading token so ``roles`` (team) never collides with ``role``.
DEFAULT_AXES: tuple[str, ...] = (
    "role", "objective", "context", "team", "rules", "review", "start",
)
AXIS_LABELS = {
    "role": "ROLE", "objective": "OBJ", "context": "CTX", "team": "TEAM",
    "rules": "RULES", "review": "REV", "start": "START",
}
_AXIS_ALIASES: dict[str, frozenset[str]] = {
    "role": frozenset({
        "role", "persona", "identity", "audience", "about", "who", "overview", "intro",
        "introduction", "you",
    }),
    "objective": frozenset({
        "objective", "objectives", "goal", "goals", "task", "tasks", "mission",
        "purpose", "outcome", "outcomes", "deliverable", "deliverables", "scope",
    }),
    "context": frozenset({
        "context", "background", "sources", "inputs", "input", "resources",
        "environment", "knowledge", "data", "reference", "references", "architecture",
        "model", "codebase",
    }),
    "team": frozenset({
        "roles", "team", "teams", "owners", "owner", "ownership", "responsibilities",
        "agents", "stakeholders", "people", "delegation", "subagents", "raci",
    }),
    "rules": frozenset({
        "rules", "rule", "constraints", "guidelines", "guardrails", "policy",
        "policies", "requirements", "conventions", "boundaries", "principles",
        "gotchas", "non-negotiable", "dos", "donts", "safety", "security",
    }),
    "review": frozenset({
        "review", "reviews", "verification", "verify", "validation", "quality",
        "checks", "check", "evaluation", "eval", "testing", "tests", "acceptance",
        "criteria", "qa", "audit",
    }),
    "start": frozenset({
        "start", "begin", "quickstart", "setup", "install", "steps", "step",
        "workflow", "process", "plan", "commands", "usage", "first", "next",
        "onboarding", "delivery", "protocol",
    }),
}
#: Body signals for documents without a mapped heading for an axis (partial credit).
_AXIS_SIGNALS: dict[str, tuple[str, ...]] = {
    "role": ("you are", "act as", "your role", "acting as", "persona"),
    "objective": ("goal", "objective", "deliver", "outcome", "the task", "purpose"),
    "context": ("context", "background", "source", "repository", "codebase"),
    "team": ("owner", "responsible", "team", "agent", "reviewer"),
    "rules": ("must", "never", "always", "do not", "don't", "only"),
    "review": ("review", "verify", "check", "test", "validate"),
    "start": ("start", "first", "begin", "step 1", "then"),
}

_OWNER_WORDS = frozenset({
    "owner", "owners", "lead", "leads", "chief", "staff", "team", "researcher",
    "researchers", "producer", "producers", "publisher", "publishers", "editor",
    "editors", "reviewer", "reviewers", "agent", "agents", "subagent", "subagents",
    "user", "users", "maintainer", "maintainers", "engineer", "engineers", "analyst",
    "analysts", "manager", "managers", "designer", "designers", "writer", "writers",
    "operator", "operators", "admin", "admins", "customer", "customers", "client",
    "clients", "human", "humans", "author", "authors", "assistant", "parent",
    "worker", "workers", "you", "we", "studio", "officer", "approver", "approvers",
})
_APPROVAL_WORDS = frozenset({
    "approve", "approves", "approved", "approval", "approvals", "confirm", "confirms",
    "confirmed", "confirmation", "consent", "permission", "permissions", "authorize",
    "authorized", "authorization", "sign-off", "signoff", "ask", "asks", "purchase",
    "purchases", "buy", "buying", "spend", "spending", "credits", "publish",
    "publishing", "deploy", "deploying", "merge", "delete", "deleting", "messages",
    "send", "sending", "payment", "payments", "release", "releases", "force-push",
})
_SOURCE_WORDS = frozenset({
    "source", "sources", "file", "files", "transcript", "transcripts", "doc", "docs",
    "document", "documents", "log", "logs", "dataset", "datasets", "repo", "repository",
    "link", "links", "url", "urls", "reference", "references", "readme", "changelog",
    "schema", "database", "manifest", "folder", "path", "paths", "api",
})
_CLAIM_WORDS = frozenset({
    "claim", "claims", "fact", "facts", "evidence", "traceable", "trace", "traces",
    "cite", "cites", "cited", "citation", "citations", "proof", "timestamp",
    "timestamps", "quote", "quotes", "verified", "accurate", "provenance", "grounded",
})
_ACTION_WORDS = frozenset({
    "write", "read", "verify", "report", "keep", "ship", "run", "build", "draft",
    "check", "review", "test", "deliver", "extract", "cover", "propose", "match",
    "create", "update", "fix", "implement", "store", "recall", "remember", "call",
    "use", "add", "remove", "commit", "push", "summarize", "document", "flag",
    "escalate", "measure", "record", "prefer", "avoid", "lint", "install",
})
#: Unbounded or subjective wording. Only explicit count, cadence, or
#: duration phrases qualify it (e.g. "every 5 minutes" is precise).
_VAGUE_WORDS = frozenset({
    "every", "everything", "all", "any", "well", "good", "better", "best", "nice", "proper", "properly",
    "appropriate", "appropriately", "reasonable", "reasonably", "robust", "clean",
    "simple", "simply", "quickly", "fast", "soon", "smoothly", "asap", "etc", "various",
    "several", "many", "few", "often", "sometimes", "usually", "maybe", "perhaps",
    "probably", "stuff", "things", "thing", "somehow", "whatever", "great",
    "seamless", "seamlessly", "intuitive", "user-friendly", "efficient", "optimal",
    "significant", "significantly", "relevant", "sufficient", "enough", "high-quality",
    "nicely", "correctly", "obviously", "basically", "lots", "mostly",
})
_ASSERTION_WORDS = frozenset({
    "is", "are", "was", "were", "has", "have", "uses", "runs", "supports", "requires",
    "must", "will", "never", "always", "contains", "stores", "returns", "owns",
    "ships", "lives", "depends",
})
_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it",
    "of", "on", "or", "that", "the", "this", "to", "was", "were", "with", "not", "no",
})

_HEADING_MD = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.+?)[ \t#]*$")
_HEADING_NUM = re.compile(r"^[ \t]{0,3}(\d{1,2})[.)][ \t]+([^\n]{1,60})$")
_HEADING_LABEL = re.compile(r"^[ \t]{0,3}([A-Za-z][A-Za-z /&'\-]{1,40}):[ \t]*$")
_XML_OPEN = re.compile(r"^[ \t]*<([A-Za-z][A-Za-z0-9_\-]{0,40})>[ \t]*$")
_XML_CLOSE = re.compile(r"^[ \t]*</([A-Za-z][A-Za-z0-9_\-]{0,40})>[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\n]*)")
_INLINE_MARKER = re.compile(r"`+|\n[ \t\r]*\n")
_TOKEN = re.compile(
    r"https?://[^\s<>()\"'`]+"
    r"|(?<![\w.\-])(?:\w[\w.\-]*|\.{1,2})(?:[/\\](?:\w[\w.\-]*|\.{1,2}))+"
    r"|[^\W_]+(?:['\u2019.\-][^\W_]+)*",
    re.UNICODE,
)
_NUMBERISH = re.compile(r"\d")
_HEADING_WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


class SpecCrawlError(ValueError):
    """Raised for invalid spec-crawl input (size, type, or template)."""


class SpecClassifier(Protocol):
    """Classifies one token. Implementations must be deterministic and side-effect free."""

    def classify(self, token: str, *, in_code: bool) -> str:
        ...


#: ``support_lookup(claim_text)`` returns ``{"id": ..., "support": float}`` when a memory
#: in scope sufficiently supports the claim, else ``None``. The caller owns scoping,
#: authorization, and the support floor; this module only records the outcome.
SupportLookup = Callable[[str], Optional[Mapping[str, Any]]]


class LexiconClassifier:
    """The offline default: a small, explainable lexicon plus shape rules."""

    def classify(self, token: str, *, in_code: bool) -> str:
        lower = token.casefold()
        if lower.startswith(("http://", "https://")) or "/" in token or "\\" in token:
            return "source"
        if re.search(r"\.(md|py|js|ts|json|toml|ya?ml|txt|db|sql|html|css)$", lower):
            return "source"
        if _NUMBERISH.search(token):
            return "spec"
        if in_code:
            return "word"
        if lower in _VAGUE_WORDS:
            return "vague"
        if lower in _APPROVAL_WORDS:
            return "approval"
        if lower in _OWNER_WORDS:
            return "owner"
        if lower in _CLAIM_WORDS:
            return "claim"
        if lower in _SOURCE_WORDS:
            return "source"
        if lower in _ACTION_WORDS:
            return "action"
        return "word"


@dataclass(frozen=True)
class SpecTemplate:
    """Expected axes for coverage. Aliases default to the built-in heading aliases."""

    name: str = "studio"
    axes: tuple[str, ...] = DEFAULT_AXES
    aliases: Mapping[str, frozenset[str]] = field(default_factory=lambda: dict(_AXIS_ALIASES))

    def axis_for(self, heading: str) -> str:
        words = [w.casefold() for w in _HEADING_WORD.findall(heading)]
        for word in words:
            for axis in self.axes:
                if word in self.aliases.get(axis, frozenset()):
                    return axis
        return "other"


DEFAULT_TEMPLATE = SpecTemplate()


@dataclass
class _Section:
    index: int
    title: str
    axis: str
    heading_start: int
    start: int
    end: int


@dataclass
class _Token:
    text: str
    start: int
    end: int
    kind: str
    section: int
    sentence: int
    in_code: bool


def _validate(text: object) -> str:
    if not isinstance(text, str):
        raise SpecCrawlError("spec text must be a string")
    if len(text) > MAX_SPEC_CHARS:
        raise SpecCrawlError(f"spec text exceeds {MAX_SPEC_CHARS} characters")
    if not text.strip():
        raise SpecCrawlError("spec text is empty")
    # NFC keeps offsets stable for already-normalized text while canonicalizing the rest.
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Fenced blocks and inline backtick spans. Code is read but never flagged."""
    ranges: list[tuple[int, int]] = []
    offset = 0
    fence_start: Optional[int] = None
    fence_marker = ""
    fence_length = 0
    for line in text.split("\n"):
        fence = _FENCE.match(line)
        if fence:
            marker, trailing = fence.groups()
            if fence_start is None:
                # Backtick info strings cannot themselves contain backticks.
                if marker[0] != "`" or "`" not in trailing:
                    fence_start = offset
                    fence_marker, fence_length = marker[0], len(marker)
            elif (marker[0] == fence_marker and len(marker) >= fence_length
                  and not trailing.strip(" \t\r")):
                ranges.append((fence_start, offset + len(line)))
                fence_start = None
        offset += len(line) + 1
    if fence_start is not None:
        ranges.append((fence_start, len(text)))
    # Inline spans cannot cross a fenced block, even if its body has backticks.
    prose_start = 0
    for fence_start, fence_end in tuple(ranges):
        ranges.extend(_inline_code_ranges(text, prose_start, fence_start))
        prose_start = fence_end
    ranges.extend(_inline_code_ranges(text, prose_start, len(text)))
    merged: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _inline_code_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Match equal backtick runs within prose paragraphs in linear time."""
    markers = list(_INLINE_MARKER.finditer(text, start, end))
    opener_starts = [marker.start() for marker in markers]
    next_closers = [-1] * len(markers)
    by_length: dict[int, int] = {}
    # Precompute the next compatible closer so unmatched runs never rescan text.
    for index in range(len(markers) - 1, -1, -1):
        marker = markers[index]
        if text[marker.start()] != "`":
            by_length.clear()  # a blank line ends the prose paragraph
            continue
        preceding = marker.start() - 1
        while preceding >= start and text[preceding] == "\\":
            preceding -= 1
        if (marker.start() - preceding - 1) % 2:
            # Outside code, an escape consumes only the first tick of a run.
            opener_starts[index] += 1
        next_closers[index] = by_length.get(marker.end() - opener_starts[index], -1)
        # Inside code, backslashes are literal and do not escape a closing run.
        by_length[marker.end() - marker.start()] = index
    ranges = []
    index = 0
    while index < len(markers):
        closer = next_closers[index]
        if closer < 0:
            index += 1
            continue
        ranges.append((opener_starts[index], markers[closer].end()))
        index = closer + 1
    return ranges


def _in_ranges(pos: int, ranges: Sequence[tuple[int, int]]) -> bool:
    index = bisect_right(ranges, (pos, float("inf"))) - 1
    return index >= 0 and pos < ranges[index][1]


def _mask_code_ranges(text: str, ranges: Sequence[tuple[int, int]]) -> str:
    """Exclude code without joining surrounding clauses or changing offsets."""
    if not ranges:
        return text
    masked = list(text)
    for start, end in ranges:
        masked[start:end] = ["\n" if char == "\n" else " " for char in text[start:end]]
        masked[start] = ";"  # a removed inline example cannot supply a missing argument
    return "".join(masked)


def _parse_sections(text: str, template: SpecTemplate,
                    code: Sequence[tuple[int, int]], *,
                    visible_text: Optional[str] = None) -> list[_Section]:
    heads: list[tuple[int, int, str]] = []  # (heading_start, body_start, title)
    if visible_text is None:
        visible_text = _mask_code_ranges(text, code)
    offset = 0
    lines = text.split("\n")
    for n, line in enumerate(lines):
        line_end = offset + len(line)
        title: Optional[str] = None
        if not _in_ranges(offset, code):
            # Numbered and "Label:" headings are only headings when they open a block;
            # otherwise wrapped prose ending in ":" or a short list item would split.
            block_start = n == 0 or not lines[n - 1].strip()
            next_line = lines[n + 1] if n + 1 < len(lines) else ""
            match = _HEADING_MD.match(line) or _XML_OPEN.match(line)
            if match is None and block_start:
                label = _HEADING_LABEL.match(line)
                numbered = _HEADING_NUM.match(line)
                if label:
                    match = label
                elif (numbered and len(numbered.group(2).split()) <= 6
                      and not numbered.group(2).rstrip().endswith((".", ",", ";"))
                      and not _HEADING_NUM.match(next_line)):
                    match = numbered
            if match:
                title = match.group(match.lastindex or 1).strip()
            elif _XML_CLOSE.match(line):
                title = ""  # closing tag: end the section, retain any following prose
        if title is not None:
            heads.append((offset, min(line_end + 1, len(text)), title))
        offset = line_end + 1
    sections: list[_Section] = []
    first_head = heads[0][0] if heads else len(text)
    if text[:first_head].strip():
        sections.append(_Section(0, "preamble", "other", 0, 0, first_head))
    for i, (head_start, body_start, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(text)
        if not title:
            if text[body_start:end].strip():
                sections.append(_Section(len(sections), "continuation", "other",
                                         head_start, body_start, end))
            continue
        clean = re.sub(r"^[\d.)\s]+", "", title).strip(" *_`") or title
        axis = template.axis_for(visible_text[head_start:body_start])
        sections.append(_Section(len(sections), clean[:80], axis,
                                 head_start, body_start, end))
    if not sections:
        sections.append(_Section(0, "spec", "other", 0, 0, len(text)))
    if len(sections) > MAX_SECTIONS:
        tail = sections[MAX_SECTIONS - 1]
        tail.end = sections[-1].end
        sections = sections[:MAX_SECTIONS]
    for i, section in enumerate(sections):
        section.index = i
    return sections


def _tokenize(text: str, sections: Sequence[_Section], code: Sequence[tuple[int, int]],
              classifier: SpecClassifier) -> tuple[list[_Token], bool]:
    tokens: list[_Token] = []
    sentence = 0
    truncated = False
    for section in sections:
        prev_end = section.start
        for match in _TOKEN.finditer(text, section.start, section.end):
            if len(tokens) >= MAX_SPEC_WORDS:
                truncated = True
                break
            raw = match.group(0).rstrip(".,;:!?)]}")
            if not raw:
                continue
            gap = text[prev_end:match.start()]
            if prev_end == section.start or re.search(r"[.!?;]|\n", gap):
                sentence += 1
            in_code = _in_ranges(match.start(), code)
            kind = classifier.classify(raw, in_code=in_code)
            if kind not in TOKEN_KINDS:
                kind = "word"
            tokens.append(_Token(raw, match.start(), match.start() + len(raw), kind,
                                 section.index, sentence, in_code))
            prev_end = match.start() + len(raw)
        if truncated:
            break
    return tokens, truncated


_BOUND_NUMBER = r"\d+(?:,\d{3})*"
_COUNT_BOUND = re.compile(
    rf"[ \t]+(?:(?:of[ \t]+)?the[ \t]+|of[ \t]+)?{_BOUND_NUMBER}"
    r"(?=$|[ \t);:!?]|\.(?!\d))", re.IGNORECASE,
)
_CADENCE_BOUND = re.compile(
    rf"[ \t]+{_BOUND_NUMBER}(?:\.\d+)?[ \t]*(?P<unit>[a-zA-Z][a-zA-Z-]*)"
    r"(?=$|[ \t.,;:!?])", re.IGNORECASE,
)
_DURATION_BOUND = re.compile(
    rf"[ \t]+(?:within|under|in|at[ \t]+most|no[ \t]+more[ \t]+than)"
    rf"[ \t]+{_BOUND_NUMBER}(?:\.\d+)?[ \t]*(?P<unit>[a-zA-Z]+)"
    r"(?=$|[ \t.,;:!?])", re.IGNORECASE,
)
_TEMPORAL_VAGUE = frozenset({"quickly", "fast", "soon", "asap"})
_DURATION_UNITS = frozenset({
    "ms", "msec", "msecs", "millisecond", "milliseconds", "s", "sec", "secs",
    "second", "seconds", "min", "mins", "minute", "minutes", "h", "hr", "hrs",
    "hour", "hours", "day", "days", "week", "weeks",
})
_CADENCE_BOUNDARIES = _STOPWORDS | _VAGUE_WORDS | {
    "but", "then", "because", "while", "when", "if", "else", "after", "before",
    "since", "until", "unless",
}


def _apply_vague_qualifiers(tokens: list[_Token], text: str) -> None:
    """Clear only explicit quantitative phrases within a bounded prose span."""
    for i, tok in enumerate(tokens):
        if tok.kind != "vague":
            continue
        word = tok.text.casefold()
        if word in {"all", "any"}:
            pattern = _COUNT_BOUND
        elif word == "every":
            pattern = _CADENCE_BOUND
        elif word in _TEMPORAL_VAGUE:
            pattern = _DURATION_BOUND
        else:
            continue
        following = tokens[i + 1:i + 1 + VAGUE_QUALIFIER_WINDOW]
        if not following:
            continue
        match = pattern.match(text, tok.end, following[-1].end)
        if match is None:
            continue
        unit = (match.groupdict().get("unit") or "").casefold()
        if (word == "every" and unit in _CADENCE_BOUNDARIES
                or word in _TEMPORAL_VAGUE and unit not in _DURATION_UNITS):
            continue
        if all(not other.in_code and other.section == tok.section
               and other.sentence == tok.sentence
               for other in following if other.start < match.end()):
            tok.kind = "word"


def _sentence_spans(tokens: Sequence[_Token]) -> dict[int, tuple[int, int, int]]:
    spans: dict[int, tuple[int, int, int]] = {}
    for tok in tokens:
        start, end, section = spans.get(tok.sentence, (tok.start, tok.end, tok.section))
        spans[tok.sentence] = (min(start, tok.start), max(end, tok.end), section)
    return spans


def _excerpt(text: str, start: int, end: int, limit: int = 160) -> str:
    snippet = " ".join(text[start:end].split())
    return snippet if len(snippet) <= limit else snippet[: limit - 1].rstrip() + "\u2026"


def _claim_sentences(text: str, tokens: Sequence[_Token]) -> list[tuple[int, int, int, int]]:
    """Declarative sentences (â‰¥4 words) carrying an assertion marker or a spec value."""
    by_sentence: dict[int, list[_Token]] = {}
    for tok in tokens:
        if not tok.in_code:
            by_sentence.setdefault(tok.sentence, []).append(tok)
    out = []
    for sid, toks in by_sentence.items():
        if len(toks) < 4:
            continue
        start, end = toks[0].start, toks[-1].end
        tail = text[end:end + 2]
        if "?" in tail:
            continue
        lowers = {t.text.casefold() for t in toks}
        if lowers & _ASSERTION_WORDS or any(t.kind in {"spec", "claim"} for t in toks):
            out.append((sid, start, end, toks[0].section))
    return out


def score_spec(*, coverage: float, ownership: float, approvals: float,
               traced_ratio: Optional[float], vague_flags: int, untraced_flags: int,
               injection_flags: int) -> tuple[int, dict[str, float]]:
    """Bounded 0â€“100 spec score. More flags never raise the score.

    ``100Â·(0.45Â·coverage + 0.20Â·ownership + 0.15Â·approvals + 0.20Â·traced)`` minus a
    capped penalty (2 per vague up to 24, 3 per untraced, 10 per injection; total cap 40). When
    claims were not checked, the traced term is dropped and the rest re-normalized.
    """
    def unit(value: float) -> float:
        return max(0.0, min(1.0, float(value)))

    parts = {"coverage": 0.45 * unit(coverage), "ownership": 0.20 * unit(ownership),
             "approvals": 0.15 * unit(approvals)}
    weight = 0.80
    if traced_ratio is not None:
        parts["traced"] = 0.20 * unit(traced_ratio)
        weight = 1.0
    base = 100.0 * sum(parts.values()) / weight
    penalty = min(40.0, min(24.0, 2.0 * max(0, vague_flags)) + 3.0 * max(0, untraced_flags)
                  + 10.0 * max(0, injection_flags))
    score = int(round(max(0.0, min(100.0, base - penalty))))
    breakdown = {k: round(100.0 * v / weight, 2) for k, v in parts.items()}
    breakdown["penalty"] = round(-penalty, 2)
    return score, breakdown


def _question(kind: str, token: str, sentence: str, detail: str = "") -> str:
    if kind == "vague":
        return (f'"{token}" is unbounded in: "{sentence}". What exact scope, number, '
                f"or condition do you mean?")
    if kind == "untraced":
        return f'No memory in scope supports: "{sentence}". What is the source?'
    return (f"This section contains instruction-like payload signals ({detail}). "
            f"Is that content intended to be part of the spec?")


def crawl_spec(text: str, *, template: SpecTemplate = DEFAULT_TEMPLATE,
               classifier: Optional[SpecClassifier] = None,
               support_lookup: Optional[SupportLookup] = None,
               include_trace: bool = True, source_label: str = "") -> dict[str, Any]:
    """Crawl ``text`` and return a JSON-ready report (and replay trace).

    Deterministic for identical ``text``/``template``/classifier and lookup results.
    ``support_lookup`` is called at most ``MAX_TRACED_CLAIMS`` times; exceptions from
    it are treated as "unchecked" so a failing memory backend never breaks the crawl.
    """
    text = _validate(text)
    classifier = classifier or LexiconClassifier()
    code = _code_ranges(text)
    sections = _parse_sections(text, template, code)
    tokens, truncated = _tokenize(text, sections, code, classifier)
    _apply_vague_qualifiers(tokens, text)
    spans = _sentence_spans(tokens)

    trace: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    flag_counts: Counter[str] = Counter()
    per_section = [Counter() for _ in sections]
    seen_terms: dict[str, int] = {}  # term -> section index where first linked
    links = cross_links = 0
    last_linked: dict[int, int] = {}  # sentence -> token index
    current_section = -1

    def emit(verb: str, **payload: Any) -> None:
        if include_trace:
            trace.append({"verb": verb, **payload})

    def add_flag(kind: str, token: str, start: int, end: int, section: int,
                 sentence_span: tuple[int, int], detail: str = "") -> None:
        flag_counts[kind] += 1
        per_section[section]["flagged"] += 1
        if len(flags) >= MAX_FLAGS:
            if kind != "injection":
                return
            replace_at = next((i for i in range(len(flags) - 1, -1, -1)
                               if flags[i]["kind"] != "injection"), None)
            if replace_at is None:
                return
            flags.pop(replace_at)
        sentence = _excerpt(text, *sentence_span)
        flags.append({
            "id": f"flag_{sum(flag_counts.values()):03d}", "kind": kind, "token": token,
            "section": section, "start": start, "end": end,
            "sentence": sentence, "question": _question(kind, token, sentence, detail),
        })
        emit("flag", section=section, token=token, kind=kind, start=start, end=end)

    for i, tok in enumerate(tokens):
        if tok.section != current_section:
            current_section = tok.section
            emit("walk", section=tok.section, token=sections[tok.section].title,
                 kind="section", start=sections[tok.section].heading_start,
                 end=sections[tok.section].start)
        counts = per_section[tok.section]
        counts["read"] += 1
        if not tok.in_code:
            counts["prose"] += 1
        counts[tok.kind] += 1
        sentence_span = spans[tok.sentence][:2]
        if tok.kind == "vague":
            add_flag("vague", tok.text, tok.start, tok.end, tok.section, sentence_span)
            continue
        if tok.kind not in LINKED_KINDS:
            emit("read", section=tok.section, token=tok.text, kind=tok.kind,
                 start=tok.start, end=tok.end)
            continue
        counts["linked"] += 1
        term = tok.text.casefold()
        link_to = last_linked.get(tok.sentence)
        cross_from = seen_terms.get(term)
        cross = cross_from is not None and cross_from != tok.section
        if link_to is not None:
            links += 1
        if cross:
            cross_links += 1
        seen_terms.setdefault(term, tok.section)
        last_linked[tok.sentence] = i
        emit("link", section=tok.section, token=tok.text, kind=tok.kind,
             start=tok.start, end=tok.end, link_to=link_to,
             cross_from=cross_from if cross else None)

    # Code examples are read but never scored as active instructions.
    injection_text = _mask_code_ranges(text, code)
    for section in sections:
        # Detection returns signal codes rather than offsets. Compact whitespace
        # here so a masked example cannot trigger quadratic whitespace searches;
        # retain the full offset-preserving text for every other analysis pass.
        signals = detect_payload_signals(
            " ".join(injection_text[section.start:section.end].split()),
            title=" ".join(injection_text[section.heading_start:section.start].split()),
        )
        if signals:
            add_flag("injection", signals[0], section.start, section.end, section.index,
                     (section.start, min(section.end, section.start + 160)),
                     detail=", ".join(signals))

    # Claim tracing ("does every claim trace?").
    claims: list[dict[str, Any]] = []
    checked = traced = attempts = 0
    for sid, start, end, section in _claim_sentences(text, tokens):
        sentence = _excerpt(injection_text, start, end, limit=400)
        status, memory_id, support = "unchecked", None, None
        if support_lookup is not None and attempts < MAX_TRACED_CLAIMS:
            attempts += 1
            try:
                hit = support_lookup(sentence)
            except Exception:  # noqa: BLE001 â€” a failing backend must not break a crawl
                hit, status = None, "unchecked"
            else:
                checked += 1
                if hit and hit.get("id"):
                    traced += 1
                    status, memory_id = "traced", str(hit["id"])
                    support = round(float(hit.get("support") or 0.0), 4)
                else:
                    status = "untraced"
        claims.append({"section": section, "start": start, "end": end,
                       "sentence": _excerpt(text, start, end), "status": status,
                       "memory_id": memory_id, "support": support})
        if status == "traced":
            emit("trace", section=section, token=memory_id, kind="traced",
                 start=start, end=end)
        elif status == "untraced":
            add_flag("untraced", _excerpt(text, start, end, limit=40), start, end,
                     section, (start, end))

    # Coverage radar.
    body_lower = ";\n".join(
        injection_text[section.start:section.end] for section in sections
    ).casefold()
    body_words = set(re.findall(r"\w+", body_lower))
    axes: dict[str, float] = {}
    for axis in template.axes:
        mapped = [s for s in sections if s.axis == axis]
        words = sum(per_section[s.index]["prose"] for s in mapped)
        section_score = min(1.0, words / 8.0) if mapped else 0.0
        signal_score = 0.5 if any(
            _contains_axis_term(sig, body_lower, body_words)
            for sig in _AXIS_SIGNALS.get(axis, ())
        ) else 0.0
        axes[axis] = round(max(section_score, signal_score), 4)
    coverage = sum(axes.values()) / len(axes) if axes else 0.0

    totals: Counter = Counter()
    for counts in per_section:
        totals.update(counts)
    owner_terms = {t.text.casefold() for t in tokens if t.kind == "owner"}
    approval_terms = {t.text.casefold() for t in tokens if t.kind == "approval"}
    kind_flags = flag_counts
    score, breakdown = score_spec(
        coverage=coverage,
        ownership=min(1.0, len(owner_terms) / 3.0),
        approvals=min(1.0, len(approval_terms) / 2.0),
        traced_ratio=(traced / checked) if checked else None,
        vague_flags=kind_flags["vague"], untraced_flags=kind_flags["untraced"],
        injection_flags=kind_flags["injection"],
    )

    section_rows = []
    for section in sections:
        counts = per_section[section.index]
        section_rows.append({
            "index": section.index, "title": section.title, "axis": section.axis,
            "intent": _section_intent(text, section, tokens),
            "start": section.start, "end": section.end,
            "words": counts["read"], "linked": counts["linked"],
            "flagged": counts["flagged"],
            "kinds": {kind: counts[kind] for kind in TOKEN_KINDS if counts[kind]},
        })

    report: dict[str, Any] = {
        "algorithm_version": ALGORITHM_VERSION,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "source_label": str(source_label or "")[:200],
        "template": template.name,
        "axes_order": list(template.axes),
        "axis_labels": {a: AXIS_LABELS.get(a, a.upper()[:5]) for a in template.axes},
        "legs": LEGS,
        "truncated": truncated,
        "word_count": len(tokens),
        "sections": section_rows,
        "coverage": axes,
        "counts": {
            "read": totals["read"], "linked": totals["linked"],
            "flagged": len(flags), "guessed": 0, "links": links,
            "cross_links": cross_links, "claims": len(claims),
            "claims_checked": checked, "claims_traced": traced,
            "claim_lookup_attempts": attempts, "flagged_total": sum(flag_counts.values()),
            "owners": totals["owner"], "approvals": totals["approval"],
            "kinds": {kind: totals[kind] for kind in TOKEN_KINDS},
        },
        "score": score,
        "score_breakdown": breakdown,
        "flags": flags,
        "claims": claims,
    }
    if include_trace:
        report["trace"] = trace
        report["trace_sha256"] = hashlib.sha256(
            repr([(e["verb"], e.get("section"), e.get("token"), e.get("start"))
                  for e in trace]).encode("utf-8"),
        ).hexdigest()
    return report


def _section_intent(text: str, section: _Section, tokens: Sequence[_Token]) -> str:
    for tok in tokens:
        if tok.section == section.index and not tok.in_code:
            line_end = text.find("\n", tok.start)
            line_end = section.end if line_end == -1 else min(line_end, section.end)
            return _excerpt(text, tok.start, line_end, limit=80)
    return ""


def compose_memory_spec(memories: Iterable[Mapping[str, Any]], *, limit: int = 200) -> str:
    """Render procedural memories as a crawlable Markdown spec (one section each).

    Titles become headings so section/axis mapping still applies. The companion
    composition helper can retain source IDs as metadata outside the audited text.
    """
    return compose_memory_spec_with_truncation(memories, limit=limit)[0]


def compose_memory_spec_with_truncation(
    memories: Iterable[Mapping[str, Any]], *, limit: int = 200,
    source_map: Optional[list[dict[str, Any]]] = None,
) -> tuple[str, bool]:
    """Render memories, optionally appending full IDs and canonical text ranges.

    IDs stay outside the spec so they cannot be classified as policy or claims.
    Only included, nonempty records contribute ranges; the return tuple is unchanged.
    """
    parts: list[str] = []
    total = 0
    for i, memory in enumerate(memories):
        if i >= limit:
            return "\n".join(parts), True
        title = unicodedata.normalize(
            "NFC", " ".join(str(memory.get("title") or "").split()),
        )[:80] or f"memory {i + 1}"
        content = unicodedata.normalize(
            "NFC", str(memory.get("content") or "").replace("\r\n", "\n").replace("\r", "\n"),
        ).strip()
        if not content:
            continue
        block = f"## {title}\n{content}\n"
        addition = len(block) + bool(parts)
        if total + addition > MAX_SPEC_CHARS:
            return "\n".join(parts), True
        if source_map is not None and memory.get("id") is not None:
            source_map.append({
                "memory_id": str(memory["id"]),
                "start": total + bool(parts), "end": total + addition,
            })
        parts.append(block)
        total += addition
    return "\n".join(parts), False


def apply_answer(text: str, flag: Mapping[str, Any], answer: str) -> str:
    """Return ``text`` with an answered vague token replaced by the human's wording.

    Only the exact flagged span is replaced, and only when it still matches, so a stale
    flag can never rewrite an unrelated part of the spec.
    """
    text = _validate(text)
    answer = " ".join(str(answer or "").split())
    if not answer:
        raise SpecCrawlError("answer is empty")
    try:
        start, end = int(flag["start"]), int(flag["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SpecCrawlError("flag must carry integer start/end offsets") from exc
    if not 0 <= start < end <= len(text) or text[start:end] != str(flag.get("token")):
        raise SpecCrawlError("flag no longer matches the spec text")
    return text[:start] + answer + text[end:]


_PARAM_PATTERN = re.compile(
    r"\b(database|db|port|timeout|retries|max_retries|version|model|auth|auth_mode|strategy|provider|host)\s*(=|:|\bis\s+set\s+to\b|\bset\s+to\b|\bis\b)\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_ENFORCE_PATTERN = re.compile(
    r"\b(must|always|required|enforce|mandatory)\s+([a-zA-Z_]{3,20})\b",
    re.IGNORECASE,
)
_FORBID_PATTERN = re.compile(
    r"\b(never|forbid|deprecated|do not|don't|disable|prohibit)\s+([a-zA-Z_]{3,20})\b",
    re.IGNORECASE,
)


def _contains_axis_term(term: str, text: str, words: set[str]) -> bool:
    # Most aliases/signals are individual words. Index them once instead of
    # rescanning a large memory for each term; retain phrase/hyphen matching.
    if term.isidentifier():
        return term in words
    # A phrase cannot match unless each constituent word is present. Retain the
    # exact regex check for order, separators, and word boundaries when possible.
    if any(word not in words for word in re.findall(r"\w+", term)):
        return False
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _node_axis_scores(title: str, content: str) -> Counter[str]:
    combined = f"{title} {content}".lower()
    title_words = {word.casefold() for word in _HEADING_WORD.findall(title)}
    combined_words = set(re.findall(r"\w+", combined))
    scores: Counter[str] = Counter()
    for axis, aliases in _AXIS_ALIASES.items():
        for alias in aliases:
            if alias in title_words:
                scores[axis] += 3
            elif _contains_axis_term(alias, combined, combined_words):
                scores[axis] += 1
    for axis, signals in _AXIS_SIGNALS.items():
        for sig in signals:
            if _contains_axis_term(sig, combined, combined_words):
                scores[axis] += 2
    return scores


def _classify_node_axis(title: str, content: str) -> str:
    scores = _node_axis_scores(title, content)
    return scores.most_common(1)[0][0] if scores else "context"


def _covered_node_axes(
    title: str, content: str, *, code: Optional[Sequence[tuple[int, int]]] = None,
    visible: Optional[str] = None,
) -> list[str]:
    """Keep primary labels separate from coverage supported by active policy prose."""
    if code is None:
        code = _code_ranges(content)
    if visible is None:
        visible = _mask_code_ranges(content, code)
    sections = _parse_sections(content, DEFAULT_TEMPLATE, code, visible_text=visible)
    active_parts: list[str] = []
    heading_axes: set[str] = set()
    for section in sections:
        body = visible[section.start:section.end]
        if _TOKEN.search(body) is None:
            continue
        heading = visible[section.heading_start:section.start]
        active_parts.extend((heading, body))
        heading_words = {word.casefold() for word in _HEADING_WORD.findall(heading)}
        heading_axes.update(axis for axis, aliases in _AXIS_ALIASES.items()
                            if heading_words & aliases)
    if not active_parts:
        return []
    prose = "\n".join(active_parts)
    title = _mask_code_ranges(title, _code_ranges(title))
    scores = _node_axis_scores(title, prose)
    # Three points already represent a title alias or an alias backed by a
    # body signal. A lone incidental secondary signal is not policy coverage.
    covered = {axis for axis, score in scores.items() if score >= 3}
    if not scores:
        covered.add("context")
    covered.update(heading_axes)
    if _extract_node_directives(prose):
        covered.add("rules")
    return [axis for axis in DEFAULT_AXES if axis in covered]


def _extract_node_parameters(text: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for match in _PARAM_PATTERN.finditer(text):
        key = match.group(1).lower()
        if key == "db":
            key = "database"
        val = match.group(3).strip().lower()
        numeric_key = key in {"port", "timeout", "retries", "max_retries"}
        # Plain copular prose can describe compatible properties. Only typed
        # numeric fields make a bare "is" predicate an exclusive scalar value.
        if match.group(2).lower() == "is" and not (
            numeric_key and re.fullmatch(r"-?\d+(?:\.\d+)?\.?", val)
        ):
            continue
        if numeric_key and re.fullmatch(
            r"-?\d+(?:\.\d+)?\.", val,
        ):
            val = val[:-1]
        elif key != "host" and re.fullmatch(r"[a-z_][a-z0-9_-]*\.", val):
            # A simple bare label may end a prose sentence. Dotted identifiers,
            # absolute hostnames and tokens continuing into a resource stay literal.
            following = text[match.end():match.end() + 1]
            if not following or following.isspace() or following in ",;!)]}'\"":
                val = val[:-1]
        params[key] = val
    return params


_ENFORCE_VERBS = frozenset({"must", "always", "required", "enforce", "mandatory", "require"})
_FORBID_VERBS = frozenset({"never", "forbid", "deprecated", "disable", "prohibit", "skip", "omit"})
_NEGATIVE_ACTION_VERBS = frozenset({"forbid", "disable", "prohibit", "skip", "omit"})
_POSSIBLE_ENFORCE_VERBS = _ENFORCE_VERBS | {"do", "does", "should", "may", "can"}
_DIRECTIVE_VERBS = _POSSIBLE_ENFORCE_VERBS | _FORBID_VERBS
_DIRECTIVE_BOUNDARIES = _DIRECTIVE_VERBS | {"and", "or", "but"}
_DIRECTIVE_SCOPE_WORDS = frozenset({"to", "on", "for", "in", "across", "within", "from"})


_DIRECTIVE_NEGATION = re.compile(
    r"(?<![\w./\\:@?=&%+#-])"
    r"(?:cannot|(?:do|does|must|should|may)n['\u2019]t|can['\u2019]t)"
    r"(?![\w./\\:@?=&%+#-])", re.IGNORECASE,
)
_DIRECTIVE_LEXEME = re.compile(
    r"(?P<literal>"
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s<>\"()\x60]+"
    r"|(?:[A-Za-z]:[/\\]|\.{1,2}[/\\]|[/\\]|(?<![A-Za-z0-9_.-])[A-Za-z0-9_.-]+[/\\])[^\s<>\"()\x60]*"
    r"|\[[0-9A-Fa-f:.]+\](?::[0-9]+)?)"
    r"|(?P<word>[A-Za-z_0-9]+(?:[._:/\\@%+=~#?&'\u2019-]+[A-Za-z_0-9]+)*)"
    r"|(?P<boundary>[.!?;,\n])"
)



_DIRECTIVE_ARTICLES = frozenset({"a", "an", "the"})
_DIRECTIVE_PREPOSITIONS = _DIRECTIVE_SCOPE_WORDS | {
    "into", "at", "via", "without", "with", "except",
}
_DIRECTIVE_ARTICLE_HEAD_STOPS = (
    _DIRECTIVE_PREPOSITIONS | _DIRECTIVE_ARTICLES | _DIRECTIVE_BOUNDARIES | _STOPWORDS
)


def _normalize_directive_phrase(words: Sequence[str]) -> str:
    """Canonicalize limited grammatical variants without erasing scope values."""
    normalized = [
        word for pos, word in enumerate(words)
        if not (word in _DIRECTIVE_ARTICLES and pos + 1 < len(words)
                and words[pos + 1] not in _DIRECTIVE_ARTICLE_HEAD_STOPS
                and (pos == 1 or pos > 0 and words[pos - 1] in _DIRECTIVE_PREPOSITIONS))
    ]
    if len(normalized) > 2 and normalized[0] == "deploy":
        if normalized[1] in {"to", "into"}:
            # For deployment, the bare object can name the destination.
            del normalized[1]
        else:
            # Retain the object and destination; only alias deployment's first
            # destination preposition, before source/method/condition scope.
            for pos in range(2, len(normalized) - 1):
                if normalized[pos] in _DIRECTIVE_PREPOSITIONS:
                    if normalized[pos] == "into":
                        normalized[pos] = "to"
                    break
    return " ".join(normalized)


def _extract_node_directives(text: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    # Every governing token must also occur as a substring. Reject impossible
    # text before full-word parsing; possible matches retain the exact parser.
    lowered = text.lower()
    if not any(verb in lowered for verb in _DIRECTIVE_VERBS):
        return directives
    if "'" in text or "\u2019" in text or "cannot" in lowered:
        def expand_negation(match: re.Match[str]) -> str:
            word = match.group().lower().replace("\u2019", "'")
            return "can not" if word in {"cannot", "can't"} else word[:-3] + " not"

        text = _DIRECTIVE_NEGATION.sub(expand_negation, text)
    clauses: list[list[str]] = []
    clause: list[str] = []
    for match in _DIRECTIVE_LEXEME.finditer(text):
        if match.lastgroup == "boundary":
            if clause:
                clauses.append(clause)
                clause = []
            continue
        raw = match.group()
        # Keep escaped inline delimiters from adding path separators to prose.
        if text[match.end():match.end() + 1] == "\x60":
            raw = raw.rstrip("\\")
            if not raw:
                continue
        terminal = match.lastgroup == "literal" and raw[-1:] in {".", "!", "?", ";", ","}
        if terminal:
            raw = raw.rstrip(".!?;,")
        if raw:
            # Opaque resources can be case-sensitive. Ordinary action/scope
            # words are case-insensitive, but never fold a path or URL object.
            opaque = any(char in raw for char in ".:/\\")
            clause.append(raw if opaque else raw.lower())
        if terminal and clause:
            clauses.append(clause)
            clause = []
    if clause:
        clauses.append(clause)
    for tokens in clauses:
        if _DIRECTIVE_VERBS.isdisjoint(tokens):
            continue
        consumed: set[int] = set()
        for i, tok in enumerate(tokens):
            if i in consumed:
                continue
            negated = (i + 1 < len(tokens) and tokens[i + 1] == "not") or (
                i > 0 and tokens[i - 1] == "not")
            if tok in _FORBID_VERBS or (
                tok in _POSSIBLE_ENFORCE_VERBS and negated
            ):
                action = "enforce" if tok in _NEGATIVE_ACTION_VERBS and negated else "forbid"
            elif tok in _ENFORCE_VERBS:
                action = "enforce"
            else:
                continue
            for offset in (1, 2, 3):
                if i + offset >= len(tokens):
                    break
                target = tokens[i + offset]
                if target in {"require", "enforce"}:
                    # Nested verbs inherit the governing directive, including its negation.
                    consumed.add(i + offset)
                elif target in _NEGATIVE_ACTION_VERBS:
                    consumed.add(i + offset)
                    action = "enforce" if action == "forbid" else "forbid"
                elif target in _ENFORCE_VERBS | _FORBID_VERBS:
                    break
                elif target not in _STOPWORDS:
                    # Match the governed phrase, including its object/scope,
                    # instead of treating every nearby noun as an instruction.
                    end = i + offset + 1
                    while end < len(tokens) and tokens[end] not in _DIRECTIVE_BOUNDARIES:
                        end += 1
                    phrase = _normalize_directive_phrase(tokens[i + offset:end])
                    # Keep summaries bounded without dropping any identity suffix.
                    identity = phrase if len(phrase) <= 120 else (
                        target[:56] + ":" + hashlib.sha256(phrase.encode("utf-8")).hexdigest())
                    directives[identity] = action
                    break
    return directives


def _finite_timestamp(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _conflict_remedy(
    node_a: Mapping[str, Any], node_b: Mapping[str, Any], *, shared_subject: bool,
) -> dict[str, Any]:
    if not shared_subject:
        explanation = "Confirm that both policies concern the same subject before choosing a retained memory."
    elif (node_a["valid_from"] is None or node_b["valid_from"] is None
          or node_a["valid_from"] == node_b["valid_from"]):
        explanation = "Effective dates are missing, invalid, or equal. Choose the retained memory explicitly."
    else:
        newer, older = ((node_a, node_b) if node_a["valid_from"] > node_b["valid_from"]
                        else (node_b, node_a))
        recommendation = (
            f"Retain node '{newer['id']}' with the later effective date; confirm that it "
            f"supersedes node '{older['id']}' before closing validity."
        )
        return {
            "action": "supersede",
            "keep_node": newer["id"],
            "retire_node": older["id"],
            "ordering_basis": "valid_from",
            "recommendation": recommendation,
            "detail": recommendation,
        }
    return {
        "action": "clarify",
        "candidate_nodes": [node_a["id"], node_b["id"]],
        "recommendation": explanation,
        "detail": explanation,
    }


def analyze_memory_nodes(
    memories: Sequence[Mapping[str, Any]],
    links: Optional[Sequence[Mapping[str, Any]]] = None,
    *,
    workspace: Optional[str] = None,
    include_trace: bool = True,
) -> dict[str, Any]:
    """Analyze multiple memory nodes for cross-node contradictions, orphans, policy gaps, and health.

    Provides multi-node audit beyond single-document crawling:
    1. Node classification and 7-axis mapping per memory node.
    2. Cross-node contradiction and parameter clash detection with recommended resolution.
    3. Redundancy & near-duplicate clustering.
    4. Graph orphan detection with nearest-neighbor auto-linking suggestions.
    5. 7-axis policy gap analysis (operational blind spots).
    6. Cluster health score (0-100) and actionable remediation plan.
    7. Replayable multi-node simulation trace for cybernetic visualization.
    """
    if not isinstance(memories, Sequence):
        memories = list(memories)

    if not memories:
        empty_coverage = {a: 0.0 for a in DEFAULT_AXES}
        return {
            "cluster_health_score": 0,
            "cluster_health": 0,
            "score_breakdown": {
                "coverage_points": 0,
                "connected_points": 0,
                "quality_points": 0,
                "conflict_penalty": 0,
                "orphan_penalty": 0,
                "gap_penalty": 0,
                "vague_penalty": 0,
            },
            "node_count": 0,
            "nodes": [],
            "links": [],
            "conflicts": [],
            "conflicts_total": 0,
            "conflict_limit": MAX_NODE_CONFLICTS,
            "conflicts_truncated": False,
            "conflict_remediation_count": 0,
            "conflict_remediation_limit": MAX_CONFLICT_REMEDIES,
            "conflict_remediation_truncated": False,
            "redundancies": [],
            "orphans": [],
            "policy_gaps": [
                {
                    "axis": a,
                    "label": AXIS_LABELS.get(a, a.upper()),
                    "severity": "high",
                    "description": f"No memories for {a}",
                    "remedy": {
                        "action": "create_memory",
                        "recommended_axis": a,
                        "recommendation": f"Record a procedural or semantic memory establishing {AXIS_LABELS.get(a, a.upper())} policy.",
                    },
                }
                for a in DEFAULT_AXES
            ],
            "coverage": empty_coverage,
            "radar": empty_coverage,
            "axis_labels": {a: AXIS_LABELS.get(a, a.upper()[:5]) for a in DEFAULT_AXES},
            "flags": [],
            "remediation_plan": [],
            "trace": [],
            "similarity_token_limit": MAX_MEMORY_SIMILARITY_TOKENS,
            "similarity_sampled_node_count": 0,
            "word_limit": MAX_SPEC_WORDS,
            "word_analysis_truncated_node_count": 0,
            "analysis_truncated": False,
        }

    # 1. Parse individual nodes
    parsed_nodes: list[dict[str, Any]] = []
    node_tokens_map: dict[str, set[str]] = {}
    node_tokens_len: dict[str, int] = {}
    all_flags: list[dict[str, Any]] = []
    axis_counts: Counter[str] = Counter()

    classifier = LexiconClassifier()
    for idx, m in enumerate(memories[:200]):
        m_id = str(m.get("id") or f"mem_{idx:03d}")
        title = str(m.get("title") or "").strip() or f"Memory {idx + 1}"
        content = str(m.get("content") or "").strip()
        mtype = str(m.get("mtype") or m.get("memory_type") or "semantic").lower()
        scope = str(m.get("scope") or "workspace")
        subj_key = str(m.get("subject_key") or "").strip().lower()
        claim_kind = str(m.get("claim_kind") or "").strip().lower()
        ingested_at = _finite_timestamp(m.get("ingested_at"))
        valid_from = _finite_timestamp(m.get("valid_from"))

        # Tokens & vague words
        sec = _Section(index=0, title="node", axis="context", heading_start=0,
                       start=0, end=len(content))
        code = _code_ranges(content)
        visible_content = _mask_code_ranges(content, code)
        raw_toks, word_analysis_truncated = _tokenize(
            content, [sec], code, classifier,
        )
        _apply_vague_qualifiers(raw_toks, content)
        if "\n" not in title and not _FENCE.match(title):
            # A plain title is a separate prose field. Reuse the content mask
            # instead of scanning the whole body again.
            visible_title = _mask_code_ranges(title, _code_ranges(title))
            policy_text = f"{visible_title}\n{visible_content}"
        else:
            policy_text = f"{title}\n{content}"
            policy_text = _mask_code_ranges(policy_text, _code_ranges(policy_text))
        tok_set = tokenize(policy_text)
        similarity_sampled = len(tok_set) > MAX_MEMORY_SIMILARITY_TOKENS
        if similarity_sampled:
            # A stable hash sample avoids favouring a document's opening or lexical
            # prefix. Only similarity suggestions are sampled; policy checks below
            # still inspect the complete memory text.
            tok_set = set(sorted(tok_set, key=lambda token: (
                hashlib.sha256(token.encode("utf-8")).digest(), token,
            ))[:MAX_MEMORY_SIMILARITY_TOKENS])
        node_tokens_map[m_id] = tok_set
        node_tokens_len[m_id] = len(tok_set)

        # Classify axis
        axis = _classify_node_axis(title, content)
        covered_axes = _covered_node_axes(title, content, code=code, visible=visible_content)
        axis_counts.update(covered_axes)

        # Kinds summary
        kinds_counter: Counter[str] = Counter()
        for tok in raw_toks:
            kinds_counter[tok.kind] += 1

        # Vague qualifiers
        vague_tokens = [tok for tok in raw_toks if tok.kind == "vague"]
        for vf in vague_tokens:
            if len(all_flags) >= MAX_FLAGS:
                break
            all_flags.append({
                "id": f"{m_id}_vague_{vf.start}",
                "node_id": m_id,
                "token": vf.text,
                "question": f"In memory '{title}': \"{vf.text}\" is unbounded. What exact number, scope, or condition do you mean?",
            })

        params = _extract_node_parameters(policy_text)
        directives = _extract_node_directives(policy_text)

        parsed_nodes.append({
            "id": m_id,
            "title": title,
            "content": content,
            "mtype": mtype,
            "scope": scope,
            "subject_key": subj_key,
            "claim_kind": claim_kind,
            "ingested_at": ingested_at,
            "valid_from": valid_from,
            "axis": axis,
            "covered_axes": covered_axes,
            "word_count": len(raw_toks),
            "word_count_is_lower_bound": word_analysis_truncated,
            "word_analysis_truncated": word_analysis_truncated,
            "similarity_token_count": len(tok_set),
            "similarity_sampled": similarity_sampled,
            "token_kinds": dict(kinds_counter),
            "flag_count": len(vague_tokens),
            "parameters": params,
            "directives": directives,
        })

    # Each unordered pair is compared at most once, across all analysis passes.
    similarity_cache: dict[tuple[str, str], float] = {}

    def similarity(node_a: str, node_b: str) -> float:
        pair = (min(node_a, node_b), max(node_a, node_b))
        if pair not in similarity_cache:
            similarity_cache[pair] = jaccard(node_tokens_map[node_a], node_tokens_map[node_b])
        return similarity_cache[pair]

    # 2. Graph Connectivity & Edges
    node_ids = {n["id"] for n in parsed_nodes}
    edge_map: dict[str, set[str]] = {n_id: set() for n_id in node_ids}
    resolved_links: list[dict[str, Any]] = []

    if links:
        for link_item in links:
            a, b = str(link_item.get("a", "")), str(link_item.get("b", ""))
            rel = str(link_item.get("relation") or "related_to")
            if a in node_ids and b in node_ids:
                edge_map[a].add(b)
                edge_map[b].add(a)
                resolved_links.append({"a": a, "b": b, "relation": rel, "inferred": False})

    # Infer links if sparse: prefiltered by token set size bound
    if len(resolved_links) < len(parsed_nodes):
        max_inferred = min(len(parsed_nodes) * 2, 60)
        inferred_count = 0
        for i in range(len(parsed_nodes)):
            if inferred_count >= max_inferred:
                break
            id_i = parsed_nodes[i]["id"]
            len_i = node_tokens_len[id_i]
            if len_i == 0:
                continue
            subj_i = parsed_nodes[i]["subject_key"]
            for j in range(i + 1, len(parsed_nodes)):
                if inferred_count >= max_inferred:
                    break
                id_j = parsed_nodes[j]["id"]
                if id_j in edge_map[id_i]:
                    continue
                len_j = node_tokens_len[id_j]
                if len_j == 0:
                    continue
                min_len = len_i if len_i < len_j else len_j
                max_len = len_j if len_i < len_j else len_i
                subj_match = bool(subj_i and subj_i == parsed_nodes[j]["subject_key"])
                if not subj_match and (min_len / max_len < 0.35):
                    continue
                sim = similarity(id_i, id_j)
                if sim >= 0.35 or subj_match:
                    # Inferred overlaps are suggestions, not persisted connectivity.
                    # Keep orphan degree and cluster health tied to declared links.
                    resolved_links.append({
                        "a": id_i,
                        "b": id_j,
                        "relation": "semantic_overlap" if sim >= 0.35 else "shared_subject",
                        "inferred": True,
                        "similarity": round(sim, 2),
                    })
                    inferred_count += 1

    # Identify Orphans
    orphans: list[dict[str, Any]] = []
    for node in parsed_nodes:
        n_id = node["id"]
        degree = len(edge_map[n_id])
        node["degree"] = degree
        node["is_orphan"] = degree == 0
        if degree == 0:
            len_o = node_tokens_len[n_id]
            best_id: Optional[str] = None
            best_sim = 0.0
            if len_o > 0:
                for other in parsed_nodes:
                    if other["id"] == n_id:
                        continue
                    len_oth = node_tokens_len[other["id"]]
                    if len_oth == 0:
                        continue
                    min_l = len_o if len_o < len_oth else len_oth
                    max_l = len_oth if len_o < len_oth else len_o
                    if (min_l / max_l) <= best_sim:
                        continue
                    sim = similarity(n_id, other["id"])
                    if sim > best_sim:
                        best_sim = sim
                        best_id = other["id"]
            orphans.append({
                "node_id": n_id,
                "memory_id": n_id,
                "title": node["title"],
                "suggested_link_to": best_id if best_sim >= 0.10 else None,
                "similarity": round(best_sim, 2),
            })

    # 3. Detect Cross-Node Contradictions & Redundancies using Inverted Key Maps
    conflicts: list[dict[str, Any]] = []
    redundancies: list[dict[str, Any]] = []
    conflict_pairs: set[tuple[str, str]] = set()

    # Subject/claim metadata groups facts but does not establish exclusivity.
    # Only parameter or opposing-directive evidence below can create a conflict.
    # 3a. Parameter index
    param_map: dict[tuple[str, str], dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for idx, node in enumerate(parsed_nodes):
        if not node["subject_key"]:
            continue
        for p_key, p_val in node["parameters"].items():
            param_map[(node["subject_key"], p_key)][p_val].append(idx)

    for (_subject, p_key), val_dict in param_map.items():
        if len(val_dict) < 2:
            continue
        val_list = list(val_dict.items())
        for v1_idx in range(len(val_list)):
            val_a, idxs_a = val_list[v1_idx]
            for v2_idx in range(v1_idx + 1, len(val_list)):
                val_b, idxs_b = val_list[v2_idx]
                for idx_a in idxs_a:
                    node_a = parsed_nodes[idx_a]
                    for idx_b in idxs_b:
                        node_b = parsed_nodes[idx_b]
                        if (node_a["claim_kind"] and node_b["claim_kind"]
                                and node_a["claim_kind"] != node_b["claim_kind"]):
                            continue
                        pair_key = (min(node_a["id"], node_b["id"]), max(node_a["id"], node_b["id"]))
                        if pair_key in conflict_pairs:
                            continue
                        conflict_pairs.add(pair_key)
                        if len(conflicts) >= MAX_NODE_CONFLICTS:
                            continue
                        conflicts.append({
                            "id": f"conflict_{len(conflicts) + 1}",
                            "node_a": node_a["id"],
                            "node_b": node_b["id"],
                            "title_a": node_a["title"],
                            "title_b": node_b["title"],
                            "subject": p_key,
                            "severity": "high",
                            "reason": f"Contradictory parameter value for '{p_key}': '{val_a}' vs '{val_b}'.",
                            "detail": f"Contradictory parameter value for '{p_key}': '{val_a}' vs '{val_b}'.",
                            "remedy": _conflict_remedy(node_a, node_b, shared_subject=True),
                        })

    # 3b. Directives index
    dir_map: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for idx, node in enumerate(parsed_nodes):
        for target, action in node["directives"].items():
            dir_map[target][action].append(idx)

    # Preserve potential general-policy versus scoped-exception conflicts.
    # An alias exists only when a general/universal policy actually occurs;
    # two differently scoped rules alone never acquire a shared action key.
    exact_targets = set(dir_map)
    universal_nodes: dict[str, set[int]] = defaultdict(set)
    scoped_targets: list[tuple[str, str]] = []
    for target, action_dict in list(dir_map.items()):
        words = target.split()
        # Canonical bare deployment destinations retain their scope relationship
        # to an actually present general policy; unrelated destinations stay apart.
        if (len(words) > 1 and words[0] == "deploy"
                and words[1] not in _DIRECTIVE_PREPOSITIONS):
            scoped_targets.append((target, "deploy"))
            if words[1] in {"all", "each", "every", "any"}:
                for indices in action_dict.values():
                    universal_nodes["deploy"].update(indices)
        for pos, word in enumerate(words):
            if pos == 0 or word not in _DIRECTIVE_SCOPE_WORDS:
                continue
            base = " ".join(words[:pos])
            scoped_targets.append((target, base))
            if pos + 1 < len(words) and words[pos + 1] in {"all", "each", "every", "any"}:
                for indices in action_dict.values():
                    universal_nodes[base].update(indices)
    for target, base in scoped_targets:
        if base in exact_targets or base in universal_nodes:
            for action, indices in dir_map[target].items():
                dir_map[base][action].extend(indices)

    for target, action_dict in dir_map.items():
        if "enforce" in action_dict and "forbid" in action_dict:
            for idx_a in action_dict["enforce"]:
                node_a = parsed_nodes[idx_a]
                for idx_b in action_dict["forbid"]:
                    if idx_a == idx_b:
                        continue  # scope aliases must never create self-supersession
                    node_b = parsed_nodes[idx_b]
                    exact_match = target in node_a["directives"] and target in node_b["directives"]
                    if not exact_match and not (
                        target in node_a["directives"] or target in node_b["directives"]
                        or idx_a in universal_nodes[target] or idx_b in universal_nodes[target]
                    ):
                        continue
                    if (node_a["subject_key"] and node_b["subject_key"]
                            and node_a["subject_key"] != node_b["subject_key"]):
                        continue
                    if (node_a["claim_kind"] and node_b["claim_kind"]
                            and node_a["claim_kind"] != node_b["claim_kind"]):
                        continue
                    pair_key = (min(node_a["id"], node_b["id"]), max(node_a["id"], node_b["id"]))
                    if pair_key in conflict_pairs:
                        continue
                    conflict_pairs.add(pair_key)
                    if len(conflicts) >= MAX_NODE_CONFLICTS:
                        continue
                    shared_subject = bool(exact_match and node_a["subject_key"] and
                                          node_a["subject_key"] == node_b["subject_key"])
                    detail = (
                        f"Opposing policy directives for '{target}': '{node_a['title']}' enforces while '{node_b['title']}' forbids."
                        if exact_match else f"Potential overlapping policy scopes for '{target}'; confirm whether both rules apply in the same context."
                    )
                    remedy = _conflict_remedy(node_a, node_b, shared_subject=shared_subject)
                    if not exact_match:
                        remedy["recommendation"] = remedy["detail"] = (
                            "Confirm that the policy scopes overlap before choosing a retained memory.")
                    conflicts.append({
                        "id": f"conflict_{len(conflicts) + 1}",
                        "node_a": node_a["id"],
                        "node_b": node_b["id"],
                        "title_a": node_a["title"],
                        "title_b": node_b["title"],
                        "subject": target,
                        "severity": "high" if shared_subject else "medium",
                        "reason": detail,
                        "detail": detail,
                        "remedy": remedy,
                    })

    # 3c. Redundancies pre-filtered by length ratio
    for i in range(len(parsed_nodes)):
        if len(redundancies) >= 20:
            break
        id_a = parsed_nodes[i]["id"]
        len_a = node_tokens_len[id_a]
        if len_a == 0:
            continue
        for j in range(i + 1, len(parsed_nodes)):
            if len(redundancies) >= 20:
                break
            id_b = parsed_nodes[j]["id"]
            pair_key = (min(id_a, id_b), max(id_a, id_b))
            if pair_key in conflict_pairs:
                continue
            len_b = node_tokens_len[id_b]
            if len_b == 0:
                continue
            min_l = len_a if len_a < len_b else len_b
            max_l = len_b if len_a < len_b else len_a
            if (min_l / max_l) < 0.75:
                continue
            sim = similarity(id_a, id_b)
            if sim >= 0.75:
                redundancies.append({
                    "id": f"redundancy_{len(redundancies) + 1}",
                    "node_a": id_a,
                    "node_b": id_b,
                    "title_a": parsed_nodes[i]["title"],
                    "title_b": parsed_nodes[j]["title"],
                    "similarity": round(sim, 2),
                    "remedy": {
                        "action": "consolidate",
                        "candidate_nodes": [id_a, id_b],
                        "recommendation": f"Review nodes '{id_a}' and '{id_b}' for consolidation; choose the retained fact explicitly.",
                    },
                })

    # 4. Multi-Node 7-Axis Radar & Policy Gaps
    coverage: dict[str, float] = {}
    policy_gaps: list[dict[str, Any]] = []

    for axis in DEFAULT_AXES:
        count = axis_counts[axis]
        val = min(1.0, count / 2.0)
        coverage[axis] = round(val, 2)
        if count == 0:
            severity = "high" if axis in {"rules", "review"} else "medium"
            policy_gaps.append({
                "axis": axis,
                "label": AXIS_LABELS.get(axis, axis.upper()),
                "severity": severity,
                "description": (
                    f"Workspace '{workspace or 'default'}' has 0 memories covering {AXIS_LABELS.get(axis, axis.upper())}. "
                    f"Operational gap: no {axis} policy or instructions recorded."
                ),
                "remedy": {
                    "action": "create_memory",
                    "recommended_axis": axis,
                    "recommendation": f"Record a procedural or semantic memory establishing {AXIS_LABELS.get(axis, axis.upper())} policy.",
                },
            })

    # 5. Cluster Health Score
    covered_axes_count = sum(1 for c in coverage.values() if c > 0)
    axis_points = round((covered_axes_count / len(DEFAULT_AXES)) * 40)
    connected_points = round(((len(parsed_nodes) - len(orphans)) / max(1, len(parsed_nodes))) * 30)
    clean_nodes = sum(1 for n in parsed_nodes
                      if n["flag_count"] == 0 and not n["word_analysis_truncated"])
    quality_points = round((clean_nodes / max(1, len(parsed_nodes))) * 30)

    conflict_penalty = min(45, len(conflicts) * 15)
    orphan_penalty = min(20, len(orphans) * 5)
    gap_penalty = min(24, len(policy_gaps) * 6)
    vague_penalty = min(15, len(all_flags) * 2)

    total_base = axis_points + connected_points + quality_points
    total_penalty = conflict_penalty + orphan_penalty + gap_penalty + vague_penalty
    cluster_score = max(0, min(100, total_base - total_penalty))

    # 6. Actionable Remediation Plan
    remediation_plan: list[dict[str, Any]] = []
    for c in conflicts[:MAX_CONFLICT_REMEDIES]:
        remediation_plan.append(c["remedy"])
    for o in orphans[:10]:
        if o["suggested_link_to"]:
            remediation_plan.append({
                "action": "link",
                "from_node": o["node_id"],
                "to_node": o["suggested_link_to"],
                "recommendation": f"Auto-link orphan node '{o['node_id']}' to '{o['suggested_link_to']}' (similarity: {o['similarity']}).",
            })
    for g in policy_gaps:
        remediation_plan.append(g["remedy"])

    # 7. Ordered Trace Generation (Bounded to focal nodes & edges)
    trace: list[dict[str, Any]] = []
    if include_trace:
        # Step 1: visit & classify focal nodes (conflicts + orphans + top nodes, max 24)
        focal_node_ids: set[str] = set()
        for c in conflicts[:10]:
            focal_node_ids.add(c["node_a"])
            focal_node_ids.add(c["node_b"])
        for o in orphans[:8]:
            focal_node_ids.add(o["node_id"])
        for n in parsed_nodes:
            if len(focal_node_ids) >= 20:
                break
            focal_node_ids.add(n["id"])

        focal_nodes = [n for n in parsed_nodes if n["id"] in focal_node_ids]
        for n in focal_nodes:
            trace.append({
                "verb": "node_visit",
                "node_id": n["id"],
                "mtype": n["mtype"],
                "title": n["title"],
                "axis": n["axis"],
            })
            trace.append({
                "verb": "node_classify",
                "node_id": n["id"],
                "kinds": n["token_kinds"],
            })
        # Step 2: traverse representative links (max 25)
        for link_item in resolved_links[:25]:
            trace.append({
                "verb": "edge_traverse",
                "source": link_item["a"],
                "target": link_item["b"],
                "relation": link_item["relation"],
            })
        # Step 3: detect conflicts (max 15)
        for c in conflicts[:15]:
            trace.append({
                "verb": "conflict_detect",
                "node_a": c["node_a"],
                "node_b": c["node_b"],
                "subject": c["subject"],
                "reason": c["reason"],
            })
        # Step 4: detect orphans (max 10)
        for o in orphans[:10]:
            trace.append({
                "verb": "orphan_detect",
                "node_id": o["node_id"],
                "suggested_link": o["suggested_link_to"],
            })
        # Step 5: detect gaps
        for g in policy_gaps:
            trace.append({
                "verb": "gap_detect",
                "axis": g["axis"],
                "label": g["label"],
            })

    return {
        "cluster_health_score": cluster_score,
        "cluster_health": cluster_score,
        "score_breakdown": {
            "coverage_points": axis_points,
            "connected_points": connected_points,
            "quality_points": quality_points,
            "conflict_penalty": -conflict_penalty,
            "orphan_penalty": -orphan_penalty,
            "gap_penalty": -gap_penalty,
            "vague_penalty": -vague_penalty,
        },
        "node_count": len(parsed_nodes),
        "similarity_token_limit": MAX_MEMORY_SIMILARITY_TOKENS,
        "similarity_sampled_node_count": sum(node["similarity_sampled"] for node in parsed_nodes),
        "word_limit": MAX_SPEC_WORDS,
        "word_analysis_truncated_node_count": sum(node["word_analysis_truncated"] for node in parsed_nodes),
        "analysis_truncated": (any(node["word_analysis_truncated"] for node in parsed_nodes)
                               or len(conflict_pairs) > len(conflicts)),
        "nodes": [{key: value for key, value in node.items() if key != "content"}
                  for node in parsed_nodes],
        "links": resolved_links,
        "conflicts": conflicts,
        "conflicts_total": len(conflict_pairs),
        "conflict_limit": MAX_NODE_CONFLICTS,
        "conflicts_truncated": len(conflict_pairs) > len(conflicts),
        "conflict_remediation_count": min(len(conflicts), MAX_CONFLICT_REMEDIES),
        "conflict_remediation_limit": MAX_CONFLICT_REMEDIES,
        "conflict_remediation_truncated": len(conflict_pairs) > MAX_CONFLICT_REMEDIES,
        "redundancies": redundancies,
        "orphans": orphans,
        "policy_gaps": policy_gaps,
        "coverage": coverage,
        "radar": coverage,
        "axis_labels": {a: AXIS_LABELS.get(a, a.upper()[:5]) for a in DEFAULT_AXES},
        "flags": all_flags,
        "remediation_plan": remediation_plan,
        "trace": trace,
    }
