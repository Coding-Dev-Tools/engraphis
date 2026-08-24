"""Pin the code-arm eval invariant: the fourth arm must earn its fusion slot.

Mirrors ``python -m eval.code_arm`` exactly — same fixture, same strict-lift
criteria — so a regression that silently breaks the code bridge (or lets the
text arms reach the bridged answer, collapsing the lift) fails here first.
"""
from __future__ import annotations

import eval.code_arm as code_arm


def test_code_arm_shows_deterministic_lift_over_text_arms():
    result = code_arm.evaluate()

    # The bridge always reaches the supporting memory...
    assert result["arms"]["code"] == 1.0
    # ...while both text arms miss it on the same fixture (strict lift)...
    assert result["arms"]["code"] > result["arms"]["vector"]
    assert result["arms"]["code"] > result["arms"]["lexical"]
    # ...and enabling the arm lifts the full fused pipeline over the default.
    assert result["profiles"]["code"] > result["profiles"]["balanced"]
    assert result["passed"] is True


def test_unknown_arm_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        code_arm._arm_recall([], k=5, arm="graphppr")
