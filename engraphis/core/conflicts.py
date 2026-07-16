"""Deterministic memory quality / conflict detection.

This module is intentionally standalone and offline: no LLM, no embeddings, no DB access.
It gives the write path (or a future review UI) an explainable first-pass detector for
high-value quality signals before any optional LLM adjudication:

* duplicates — same claim repeated
* refinements — same claim with extra specificity
* contradictions — same subject/predicate with incompatible polarity/value/object
* obsolescence — a newer-looking statement explicitly replaces an older one

The detector is conservative by design. It only reports when the texts share enough
subject matter or a simple assertion pattern matches. Every result cites the candidate
memory id and includes a suggested resolution, but it never mutates storage.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal, Optional, Union

from engraphis.core.interfaces import MemoryRecord
from engraphis.core.textutil import jaccard, tokenize

ConflictType = Literal["duplicate", "refinement", "contradiction", "obsolete"]

_DUPLICATE_JACCARD = 0.82
_SAME_SUBJECT_FLOOR = 0.24
_REFINEMENT_CONTAINMENT = 0.72
_OBJECT_DIFFERENCE_CEILING = 0.55

_TEMPORAL_RE = re.compile(
    r"\b(now|currently|as of|from now on|no longer|instead|replaced|switched|migrated|"
    r"deprecated|supersedes|superseded)\b",
    re.I,
)
_NEGATIVE_RE = re.compile(
    r"\b(no longer|does not|doesn't|do not|don't|never|without|disabled?|disallowed|"
    r"den(?:y|ies|ied)|forbidden|blocked|deprecated|removed|unsupported)\b",
    re.I,
)
_POSITIVE_RE = re.compile(
    r"\b(uses?|requires?|supports?|allows?|enabled?|must|should|stores?|runs on|prefers?)\b",
    re.I,
)
_NUMBER_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|secs|seconds|m|min|mins|minutes|h|hours|"
    r"rpm|rps|requests?|%|percent|mb|gb)?\b",
    re.I,
)
_ASSERTION_RE = re.compile(
    r"\b(?P<subject>[A-Za-z0-9][A-Za-z0-9_.#/+ -]{1,80}?)\s+"
    r"(?P<neg>does not|doesn't|do not|don't|no longer\s+)?"
    r"(?P<predicate>uses?|requires?|supports?|stores?|runs on|defaults? to|prefers?|"
    r"rate limit(?: is)?|timeout(?: is)?|version(?: is)?|is|are)\s+"
    r"(?P<object>[^.;\n]{1,120})",
    re.I,
)
_OBJECT_STOP_RE = re.compile(r"\b(?:for|because|after|before|with|when|while|during|if)\b", re.I)


@dataclass(frozen=True)
class Conflict:
    """One candidate memory-quality issue.

    ``severity`` is 0..1, sorted descending by :func:`detect_conflicts`. It is a triage
    score, not proof. Callers should treat it as a review/ranking signal.
    """

    type: ConflictType
    severity: float
    memory_id: str
    reason: str
    suggested_resolution: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Assertion:
    subject: set[str]
    predicate: str
    object: set[str]
    negated: bool
    raw_object: str


def detect_conflicts(
    new_memory: Union[MemoryRecord, str],
    candidates: Iterable[MemoryRecord],
    *,
    duplicate_jaccard: float = _DUPLICATE_JACCARD,
    same_subject_floor: float = _SAME_SUBJECT_FLOOR,
) -> list[Conflict]:
    """Compare one proposed/new memory against candidate existing memories.

    The algorithm is deterministic and dependency-free. It intentionally prefers
    precision over recall: unrelated memories are ignored even if they share a few common
    words, and low-confidence semantic cases are left for future LLM adjudication.
    """
    new_text = _text(new_memory)
    new_id = getattr(new_memory, "id", "") if not isinstance(new_memory, str) else ""
    new_tokens = tokenize(new_text)
    out: list[Conflict] = []
    for cand in candidates:
        if not isinstance(cand, MemoryRecord) or cand.id == new_id:
            continue
        old_text = _text(cand)
        old_tokens = tokenize(old_text)
        if not new_tokens or not old_tokens:
            continue
        overlap = jaccard(new_tokens, old_tokens)
        assertion = _assertion_conflict(new_text, old_text)
        polarity = _polarity_conflict(new_text, old_text, overlap, same_subject_floor)
        numeric = _numeric_conflict(new_text, old_text, overlap, same_subject_floor)
        refinement = _refinement(new_tokens, old_tokens, overlap)

        issues: list[Conflict] = []
        if overlap >= duplicate_jaccard:
            issues.append(_issue(
                "duplicate", 0.25 + min(0.15, overlap - duplicate_jaccard), cand.id,
                "new memory is near-duplicate of an existing memory",
                "reinforce/noop existing memory", overlap=overlap,
            ))
        if assertion:
            kind, reason, evidence = assertion
            severity = 0.9 if kind == "obsolete" else 0.82
            severity = min(1.0, severity + overlap * 0.12)
            issues.append(_issue(
                kind, severity, cand.id, reason,
                "invalidate older memory" if kind == "obsolete" else "review contradiction",
                overlap=overlap, **evidence,
            ))
        if polarity:
            issues.append(_issue(
                "obsolete" if _looks_temporal(new_text) else "contradiction",
                0.88 if _looks_temporal(new_text) else 0.78,
                cand.id,
                "same subject appears with opposite polarity",
                "invalidate older memory" if _looks_temporal(new_text) else "review contradiction",
                overlap=overlap, polarity="mismatch",
            ))
        if numeric:
            issues.append(_issue(
                "obsolete" if _looks_temporal(new_text) else "contradiction",
                0.86 if _looks_temporal(new_text) else 0.74,
                cand.id,
                "same subject appears with different numeric values",
                "invalidate older memory" if _looks_temporal(new_text) else "review value conflict",
                overlap=overlap, old_values=sorted(numeric[0]), new_values=sorted(numeric[1]),
            ))
        if refinement and not any(i.type in ("contradiction", "obsolete") for i in issues):
            issues.append(_issue(
                "refinement", 0.42, cand.id,
                "new memory appears to refine an existing fact with added detail",
                "link as refinement or supersede if authoritative", overlap=overlap,
                containment=refinement,
            ))
        if issues:
            out.append(max(issues, key=lambda i: (i.severity, _type_rank(i.type))))

    return sorted(out, key=lambda i: (-i.severity, i.type, i.memory_id))


def _issue(kind: ConflictType, severity: float, memory_id: str, reason: str,
           suggested_resolution: str, **evidence: Any) -> Conflict:
    return Conflict(type=kind, severity=round(max(0.0, min(1.0, severity)), 4),
                    memory_id=memory_id, reason=reason,
                    suggested_resolution=suggested_resolution, evidence=evidence)


def _text(memory: Union[MemoryRecord, str]) -> str:
    if isinstance(memory, str):
        return memory
    return f"{memory.title}\n{memory.content}" if memory.title else memory.content


def _looks_temporal(text: str) -> bool:
    return bool(_TEMPORAL_RE.search(text or ""))


def _polarity(text: str) -> int:
    neg = bool(_NEGATIVE_RE.search(text or ""))
    pos = bool(_POSITIVE_RE.search(text or ""))
    if neg and not pos:
        return -1
    if pos and not neg:
        return 1
    # "does not use" contains a positive verb too; negation should win.
    if neg:
        return -1
    return 0


def _polarity_conflict(new_text: str, old_text: str, overlap: float, floor: float) -> bool:
    if overlap < floor:
        return False
    return _polarity(new_text) * _polarity(old_text) == -1


def _numeric_conflict(new_text: str, old_text: str, overlap: float,
                      floor: float) -> Optional[tuple[set[str], set[str]]]:
    if overlap < floor:
        return None
    new_vals = {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(new_text or "")}
    old_vals = {_normalize_number(m.group(0)) for m in _NUMBER_RE.finditer(old_text or "")}
    if new_vals and old_vals and new_vals != old_vals:
        return old_vals, new_vals
    return None


def _normalize_number(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").lower())


def _assertions(text: str) -> list[_Assertion]:
    out: list[_Assertion] = []
    for match in _ASSERTION_RE.finditer(text or ""):
        subject = tokenize(match.group("subject"))
        pred = " ".join((match.group("predicate") or "").lower().split())
        raw_obj = _OBJECT_STOP_RE.split(match.group("object") or "", maxsplit=1)[0]
        obj = tokenize(raw_obj)
        if subject and pred and obj:
            out.append(_Assertion(subject=subject, predicate=pred.rstrip("s"), object=obj,
                                  negated=bool(match.group("neg")), raw_object=raw_obj.strip()))
    return out[:8]


def _assertion_conflict(new_text: str, old_text: str) -> Optional[tuple[ConflictType, str, dict]]:
    for new in _assertions(new_text):
        for old in _assertions(old_text):
            if new.predicate != old.predicate:
                continue
            subject_overlap = jaccard(new.subject, old.subject)
            if subject_overlap < 0.5:
                continue
            object_overlap = jaccard(new.object, old.object)
            polarity_mismatch = new.negated != old.negated
            object_mismatch = _different_objects(new.object, old.object, object_overlap)
            if not (polarity_mismatch or object_mismatch):
                continue
            kind: ConflictType = "obsolete" if _looks_temporal(new_text) else "contradiction"
            reason = (
                "new assertion appears to replace an older assertion"
                if kind == "obsolete" else
                "same subject/predicate has incompatible object or polarity"
            )
            return kind, reason, {
                "predicate": new.predicate,
                "subject_overlap": round(subject_overlap, 4),
                "object_overlap": round(object_overlap, 4),
                "old_object": old.raw_object,
                "new_object": new.raw_object,
                "polarity_mismatch": polarity_mismatch,
            }
    return None


def _different_objects(new_object: set[str], old_object: set[str], object_overlap: float) -> bool:
    if new_object == old_object:
        return False
    # If one object is just a more specific form of the other ("PASETO" vs
    # "PASETO tokens"), treat that as compatible/refinement, not contradiction.
    containment = len(new_object & old_object) / max(1, min(len(new_object), len(old_object)))
    return object_overlap < _OBJECT_DIFFERENCE_CEILING and containment < 0.8


def _refinement(new_tokens: set[str], old_tokens: set[str], overlap: float) -> Optional[float]:
    if overlap >= _DUPLICATE_JACCARD:
        return None
    if len(new_tokens) <= len(old_tokens) + 2:
        return None
    containment = len(new_tokens & old_tokens) / max(1, len(old_tokens))
    if containment >= _REFINEMENT_CONTAINMENT:
        return round(containment, 4)
    return None


def _type_rank(kind: ConflictType) -> int:
    return {"obsolete": 4, "contradiction": 3, "refinement": 2, "duplicate": 1}[kind]
