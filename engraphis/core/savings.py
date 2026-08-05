"""Pure token-savings estimation for prompt-context deliveries.

The estimator deliberately distinguishes an actual host-history baseline from the
smaller source-packing baseline used by ordinary recall.  It is an estimate of
avoided prompt context, not provider billing or end-to-end task cost.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Optional


_RELEASE_VERSION = re.compile(r"^\d+\.\d+(?:\.\d+)?(?:[-+][0-9A-Za-z.-]+)?$")


@dataclass(frozen=True)
class SavingsEstimate:
    """One explainable, content-free token-savings estimate."""

    baseline_tokens: int
    emitted_tokens: int
    saved_tokens: int
    savings_ratio: float
    basis: str
    confidence: str
    eligible: bool
    token_counter: str = "unknown"
    release_version: Optional[str] = None

    @property
    def estimated_saved_tokens(self) -> int:
        """Name used by receipt metadata for the same saved-token value."""
        return self.saved_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_tokens": self.baseline_tokens,
            "emitted_tokens": self.emitted_tokens,
            "saved_tokens": self.saved_tokens,
            "savings_ratio": self.savings_ratio,
            "basis": self.basis,
            "confidence": self.confidence,
            "eligible": self.eligible,
            "token_counter": self.token_counter,
            **({"release_version": self.release_version}
               if self.release_version else {}),
        }


def normalize_release_version(value: Any) -> Optional[str]:
    """Return a safe release label, or ``None`` for historical/unversioned data."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _RELEASE_VERSION.fullmatch(value) else None


def _count(value: Any) -> int:
    if type(value) not in (int, float):
        return 0
    if not math.isfinite(float(value)) or value < 0:
        return 0
    return int(value)


def estimate_savings(
    *,
    operation: str,
    baseline_tokens: Any,
    emitted_tokens: Any,
    token_counter: str = "unknown",
    intent: Optional[str] = None,
    adaptive_mode: Optional[str] = None,
    release_version: Optional[str] = None,
) -> SavingsEstimate:
    """Classify one delivery and compute its conservative savings estimate.

    ``adaptive_context`` has a real before/after history baseline.  The packed
    context operations use their retrieved-source total as a narrower packing
    baseline.  Ordinary full recall is not counted because callers may not inject
    its returned memories into a model prompt.
    """
    operation = str(operation or "").strip().casefold()
    intent = str(intent or "").strip().casefold()
    mode = str(adaptive_mode or "").strip().casefold()

    basis = "unclassified"
    confidence = "unknown"
    eligible = False

    if operation == "adaptive_context":
        if mode == "retrieval":
            basis, confidence, eligible = "history_retrieval", "high", True
        elif mode == "history_fallback":
            basis, confidence, eligible = "history_fallback", "medium", True
        elif mode == "history_bypass":
            basis, confidence, eligible = "history_bypass", "none", False
        elif mode == "low_confidence_abstain":
            basis, confidence, eligible = "low_confidence_abstain", "none", False
    elif operation == "recall" and intent == "recall_context":
        basis, confidence, eligible = "packed_context", "medium", True
    elif operation in {"grounded_recall", "proactive_context"}:
        basis, confidence, eligible = "packed_context", "medium", True

    baseline = _count(baseline_tokens)
    emitted = _count(emitted_tokens)
    saved = max(0, baseline - emitted) if eligible else 0
    ratio = saved / baseline if baseline else 0.0
    counter = str(token_counter or "unknown")
    return SavingsEstimate(
        baseline_tokens=baseline,
        emitted_tokens=emitted,
        saved_tokens=saved,
        savings_ratio=ratio,
        basis=basis,
        confidence=confidence,
        eligible=eligible,
        token_counter=counter,
        release_version=normalize_release_version(release_version),
    )


def annotate_usage(
    usage: dict[str, Any],
    *,
    operation: str,
    intent: Optional[str] = None,
    adaptive_mode: Optional[str] = None,
    baseline_tokens: Any = None,
    emitted_tokens: Any = None,
    release_version: Optional[str] = None,
) -> dict[str, Any]:
    """Add estimator fields to an existing public usage dictionary."""
    estimate = estimate_savings(
        operation=operation,
        intent=intent,
        adaptive_mode=adaptive_mode,
        baseline_tokens=(
            usage.get("source_tokens", 0)
            if baseline_tokens is None else baseline_tokens
        ),
        emitted_tokens=(
            usage.get("context_tokens", 0)
            if emitted_tokens is None else emitted_tokens
        ),
        token_counter=str(usage.get("token_counter") or "unknown"),
        release_version=release_version,
    )
    out = dict(usage)
    out.update({
        "baseline_tokens": estimate.baseline_tokens,
        "emitted_tokens": estimate.emitted_tokens,
        "estimated_saved_tokens": estimate.saved_tokens,
        "estimated_savings_ratio": estimate.savings_ratio,
        "savings_basis": estimate.basis,
        "savings_confidence": estimate.confidence,
        "savings_eligible": estimate.eligible,
    })
    if estimate.release_version:
        out["release_version"] = estimate.release_version
    return out
