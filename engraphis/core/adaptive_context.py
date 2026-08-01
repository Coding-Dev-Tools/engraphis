"""Host-facing adaptive context results and deterministic history fitting.

An agent host already owns the conversation or task history it is about to place
in a model prompt.  Passing that exact text to :meth:`MemoryEngine.adaptive_context`
lets Engraphis avoid needless retrieval when the history fits, while retaining a
bounded raw-history fallback when retrieved evidence is weak.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from engraphis.core.recall import RecallResult


@dataclass
class AdaptiveContextResult:
    """One explainable context-routing decision for an agent host."""

    context: str
    mode: str
    reason: str
    history_tokens: int
    context_tokens: int
    max_context_tokens: int
    retrieval_budget_tokens: int
    retrieval_support: float = 0.0
    retrieved: bool = False
    widened: bool = False
    truncated_history: bool = False
    token_counter: str = "unknown"
    recall: Optional[RecallResult] = None

    def to_dict(self) -> dict:
        """Return privacy-safe routing telemetry without duplicating source text."""
        return {
            "mode": self.mode,
            "reason": self.reason,
            "history_tokens": self.history_tokens,
            "context_tokens": self.context_tokens,
            "max_context_tokens": self.max_context_tokens,
            "retrieval_budget_tokens": self.retrieval_budget_tokens,
            "retrieval_support": round(self.retrieval_support, 4),
            "retrieved": self.retrieved,
            "widened": self.widened,
            "truncated_history": self.truncated_history,
            "token_counter": self.token_counter,
        }


def fit_recent_history(
    history: str,
    *,
    token_budget: int,
    count_tokens: Callable[[str], int],
) -> tuple[str, bool]:
    """Return the largest recent suffix that fits a hard token budget.

    A suffix is intentional: when confidence is weak, preserving the latest task
    state and corrections is safer than silently selecting scattered old turns.
    The injected counter is the same counter used by the context packer.
    """
    source = str(history or "")
    budget = max(0, int(token_budget))
    if not source or budget == 0:
        return "", bool(source)
    if int(count_tokens(source)) <= budget:
        return source, False

    low = 0
    high = len(source)
    while low < high:
        midpoint = (low + high) // 2
        if int(count_tokens(source[midpoint:])) <= budget:
            high = midpoint
        else:
            low = midpoint + 1

    fitted = source[low:].lstrip()
    # Avoid beginning in the middle of a word when the character boundary found
    # by the counter falls inside one.
    if low > 0 and low < len(source) and source[low - 1].isalnum() and source[low].isalnum():
        # Use the first Unicode whitespace boundary, not only literal spaces
        # and newlines.  Hosts may preserve tabs or other separators in raw
        # transcripts; dropping the whole suffix in that case loses usable
        # recent history even though a safe word boundary exists.
        boundary = next(
            (index for index, character in enumerate(fitted) if character.isspace()),
            -1,
        )
        fitted = fitted[boundary + 1:].lstrip() if boundary >= 0 else ""

    # A non-additive custom tokenizer can have unusual boundary behavior.  This
    # final guard preserves the hard-budget contract even for such counters.
    while fitted and int(count_tokens(fitted)) > budget:
        fitted = fitted[1:].lstrip()
    return fitted, True
