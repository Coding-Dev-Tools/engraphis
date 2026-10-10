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
VAGUE_QUALIFIER_WINDOW = 3
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
#: Unbounded or subjective wording. Flagged unless a number appears within
#: ``VAGUE_QUALIFIER_WINDOW`` tokens (e.g. "every 5 minutes" is precise).
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
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")
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
    for line in text.split("\n"):
        if _FENCE.match(line):
            if fence_start is None:
                fence_start = offset
            else:
                ranges.append((fence_start, offset + len(line)))
                fence_start = None
        offset += len(line) + 1
    if fence_start is not None:
        ranges.append((fence_start, len(text)))
    for match in re.finditer(r"`[^`\n]+`", text):
        ranges.append((match.start(), match.end()))
    return sorted(ranges)


def _in_ranges(pos: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in ranges)


def _parse_sections(text: str, template: SpecTemplate,
                    code: Sequence[tuple[int, int]]) -> list[_Section]:
    heads: list[tuple[int, int, str]] = []  # (heading_start, body_start, title)
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
                title = ""  # closing tag: consumes the line, starts no section
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
            continue
        clean = re.sub(r"^[\d.)\s]+", "", title).strip(" *_`") or title
        sections.append(_Section(len(sections), clean[:80], template.axis_for(clean),
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


def _apply_vague_qualifiers(tokens: list[_Token]) -> None:
    """A vague word bounded by a nearby number in the same sentence is precise."""
    for i, tok in enumerate(tokens):
        if tok.kind != "vague":
            continue
        lo, hi = max(0, i - VAGUE_QUALIFIER_WINDOW), min(len(tokens), i + VAGUE_QUALIFIER_WINDOW + 1)
        if any(tokens[j].kind == "spec" and tokens[j].sentence == tok.sentence
               for j in range(lo, hi) if j != i):
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
    _apply_vague_qualifiers(tokens)
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

    # Instruction-like payloads are data, flagged once per section.
    for section in sections:
        signals = detect_payload_signals(text[section.start:section.end], title=section.title)
        if signals:
            add_flag("injection", signals[0], section.start, section.end, section.index,
                     (section.start, min(section.end, section.start + 160)),
                     detail=", ".join(signals))

    # Claim tracing ("does every claim trace?").
    claims: list[dict[str, Any]] = []
    checked = traced = attempts = 0
    for sid, start, end, section in _claim_sentences(text, tokens):
        sentence = _excerpt(text, start, end, limit=400)
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
    body_lower = text.casefold()
    axes: dict[str, float] = {}
    for axis in template.axes:
        mapped = [s for s in sections if s.axis == axis]
        words = sum(per_section[s.index]["read"] for s in mapped)
        section_score = min(1.0, words / 8.0) if mapped else 0.0
        signal_score = 0.5 if any(sig in body_lower for sig in _AXIS_SIGNALS.get(axis, ())) else 0.0
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

    Titles become headings so section/axis mapping still applies; the memory id is kept
    in the heading suffix so flags can be traced back to the originating record.
    """
    parts: list[str] = []
    total = 0
    for i, memory in enumerate(memories):
        if i >= limit:
            break
        title = " ".join(str(memory.get("title") or "").split())[:80] or f"memory {i + 1}"
        content = str(memory.get("content") or "").strip()
        if not content:
            continue
        block = f"## {title}\n{content}\n"
        addition = len(block) + bool(parts)
        if total + addition > MAX_SPEC_CHARS:
            break
        parts.append(block)
        total += addition
    return "\n".join(parts)


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
    r"\b(database|db|port|timeout|retries|max_retries|version|model|auth|auth_mode|strategy|provider|host)\s*(?:=|:|\bis(?:\s+set to)?\b|\bset to\b)\s*([A-Za-z0-9._-]+)",
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


def _classify_node_axis(title: str, content: str) -> str:
    combined = f"{title} {content}".lower()
    scores: Counter[str] = Counter()
    for axis, aliases in _AXIS_ALIASES.items():
        for alias in aliases:
            if alias in title.lower():
                scores[axis] += 3
            elif re.search(r"\b" + re.escape(alias) + r"\b", combined):
                scores[axis] += 1
    for axis, signals in _AXIS_SIGNALS.items():
        for sig in signals:
            if sig in combined:
                scores[axis] += 2
    if scores:
        return scores.most_common(1)[0][0]
    return "context"


def _extract_node_parameters(text: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for match in _PARAM_PATTERN.finditer(text):
        key = match.group(1).lower()
        val = match.group(2).strip().lower()
        params[key] = val
    return params


_ENFORCE_VERBS = frozenset({"must", "always", "required", "enforce", "mandatory", "require"})
_FORBID_VERBS = frozenset({"never", "forbid", "deprecated", "disable", "prohibit", "skip", "omit"})


def _extract_node_directives(text: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    tokens = [w.lower() for w in re.findall(r"[a-zA-Z_]{2,20}", text)]
    for i, tok in enumerate(tokens):
        negated = (i + 1 < len(tokens) and tokens[i + 1] == "not") or (
            i > 0 and tokens[i - 1] == "not")
        if tok in _FORBID_VERBS or (
            tok in _ENFORCE_VERBS | {"do", "does", "should", "may", "can"} and negated
        ):
            action = "forbid"
        elif tok in _ENFORCE_VERBS:
            action = "enforce"
        else:
            continue
        for offset in (1, 2, 3):
            if i + offset < len(tokens):
                target = tokens[i + offset]
                if target not in _ENFORCE_VERBS | _FORBID_VERBS | _STOPWORDS:
                    directives[target] = action
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
        return {
            "cluster_health_score": 0,
            "score_breakdown": {"base": 0, "penalties": 0},
            "node_count": 0,
            "nodes": [],
            "links": [],
            "conflicts": [],
            "redundancies": [],
            "orphans": [],
            "policy_gaps": [
                {"axis": a, "label": AXIS_LABELS.get(a, a.upper()), "severity": "high", "description": f"No memories for {a}"}
                for a in DEFAULT_AXES
            ],
            "coverage": {a: 0.0 for a in DEFAULT_AXES},
            "flags": [],
            "remediation_plan": [],
            "trace": [],
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
        raw_toks, _ = _tokenize(content, [sec], [], classifier)
        _apply_vague_qualifiers(raw_toks)
        tok_set = tokenize(f"{title} {content}")
        node_tokens_map[m_id] = tok_set
        node_tokens_len[m_id] = len(tok_set)

        # Classify axis
        axis = _classify_node_axis(title, content)
        axis_counts[axis] += 1

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

        params = _extract_node_parameters(f"{title}\n{content}")
        directives = _extract_node_directives(f"{title}\n{content}")

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
            "word_count": len(raw_toks),
            "token_kinds": dict(kinds_counter),
            "flag_count": len(vague_tokens),
            "parameters": params,
            "directives": directives,
        })

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
                toks_i = node_tokens_map[id_i]
                toks_j = node_tokens_map[id_j]
                sim = jaccard(toks_i, toks_j)
                if sim >= 0.35 or subj_match:
                    edge_map[id_i].add(id_j)
                    edge_map[id_j].add(id_i)
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
                toks_o = node_tokens_map[n_id]
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
                    sim = jaccard(toks_o, node_tokens_map[other["id"]])
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

    # 3a. Subject key index
    subject_map: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx, node in enumerate(parsed_nodes):
        if node["subject_key"]:
            subject_map[(node["subject_key"], node["claim_kind"])].append(idx)

    for (subj, _claim_kind), indices in subject_map.items():
        if len(indices) < 2:
            continue
        for i_pos in range(len(indices)):
            idx_a = indices[i_pos]
            node_a = parsed_nodes[idx_a]
            toks_a = node_tokens_map[node_a["id"]]
            for j_pos in range(i_pos + 1, min(len(indices), i_pos + 5)):
                idx_b = indices[j_pos]
                node_b = parsed_nodes[idx_b]
                pair_key = (min(node_a["id"], node_b["id"]), max(node_a["id"], node_b["id"]))
                if pair_key in conflict_pairs:
                    continue
                sim = jaccard(toks_a, node_tokens_map[node_b["id"]])
                # Low overlap alone is insufficient evidence of contradiction.
                # Parameter and directive comparisons below can prove a clash.
                proven_clash = any(
                    key in node_b["parameters"] and value != node_b["parameters"][key]
                    for key, value in node_a["parameters"].items()
                ) or any(
                    key in node_b["directives"] and value != node_b["directives"][key]
                    for key, value in node_a["directives"].items()
                )
                if sim < 0.70 and _claim_kind and not proven_clash:
                    conflict_pairs.add(pair_key)
                    conflicts.append({
                        "id": f"conflict_{len(conflicts) + 1}",
                        "node_a": node_a["id"],
                        "node_b": node_b["id"],
                        "title_a": node_a["title"],
                        "title_b": node_b["title"],
                        "subject": subj,
                        "severity": "medium",
                        "reason": f"Divergent assertions for common subject '{subj}'.",
                        "detail": f"Divergent assertions for common subject '{subj}'.",
                        "remedy": {
                            "action": "clarify",
                            "candidate_nodes": [node_a["id"], node_b["id"]],
                            "recommendation": f"Verify whether these assertions for '{subj}' conflict before changing either memory.",
                            "detail": f"Verify whether these assertions for '{subj}' conflict before changing either memory.",
                        },
                    })

    # 3b. Parameter index
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
                        if pair_key in conflict_pairs or len(conflicts) >= 50:
                            continue
                        conflict_pairs.add(pair_key)
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

    # 3c. Directives index
    dir_map: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for idx, node in enumerate(parsed_nodes):
        for target, action in node["directives"].items():
            dir_map[target][action].append(idx)

    for target, action_dict in dir_map.items():
        if "enforce" in action_dict and "forbid" in action_dict:
            for idx_a in action_dict["enforce"][-3:]:
                node_a = parsed_nodes[idx_a]
                for idx_b in action_dict["forbid"][-3:]:
                    node_b = parsed_nodes[idx_b]
                    if (node_a["subject_key"] and node_b["subject_key"]
                            and node_a["subject_key"] != node_b["subject_key"]):
                        continue
                    if (node_a["claim_kind"] and node_b["claim_kind"]
                            and node_a["claim_kind"] != node_b["claim_kind"]):
                        continue
                    pair_key = (min(node_a["id"], node_b["id"]), max(node_a["id"], node_b["id"]))
                    if pair_key in conflict_pairs or len(conflicts) >= 50:
                        continue
                    conflict_pairs.add(pair_key)
                    shared_subject = bool(node_a["subject_key"] and
                                          node_a["subject_key"] == node_b["subject_key"])
                    conflicts.append({
                        "id": f"conflict_{len(conflicts) + 1}",
                        "node_a": node_a["id"],
                        "node_b": node_b["id"],
                        "title_a": node_a["title"],
                        "title_b": node_b["title"],
                        "subject": target,
                        "severity": "high" if shared_subject else "medium",
                        "reason": f"Opposing policy directives for '{target}': '{node_a['title']}' enforces while '{node_b['title']}' forbids.",
                        "detail": f"Opposing policy directives for '{target}': '{node_a['title']}' enforces while '{node_b['title']}' forbids.",
                        "remedy": _conflict_remedy(node_a, node_b, shared_subject=shared_subject),
                    })

    # 3d. Redundancies pre-filtered by length ratio
    for i in range(len(parsed_nodes)):
        if len(redundancies) >= 20:
            break
        id_a = parsed_nodes[i]["id"]
        len_a = node_tokens_len[id_a]
        if len_a == 0:
            continue
        toks_a = node_tokens_map[id_a]
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
            sim = jaccard(toks_a, node_tokens_map[id_b])
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
    clean_nodes = sum(1 for n in parsed_nodes if n["flag_count"] == 0)
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
    for c in conflicts[:10]:
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
        "nodes": parsed_nodes,
        "links": resolved_links,
        "conflicts": conflicts,
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
