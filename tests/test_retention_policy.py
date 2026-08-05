import math

import pytest

from engraphis.core import scoring
from engraphis.core.retention_policy import (
    MAX_ACCESS_COUNT,
    MAX_STABILITY_DAYS,
    reinforced_stability,
)


def test_reinforcement_marginal_gain_diminishes():
    stability, count = 1.0, 0
    gains = []
    for _ in range(100):
        updated, count = reinforced_stability(stability, count, boost=0.15)
        gains.append(updated - stability)
        stability = updated

    assert all(a > b > 0 for a, b in zip(gains, gains[1:]))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("alpha", float("nan")),
        ("alpha", -0.1),
        ("boost", float("inf")),
        ("boost", -0.1),
    ],
)
def test_reinforcement_rejects_invalid_strength(field, value):
    with pytest.raises(ValueError):
        reinforced_stability(1.0, 0, **{field: value})


@pytest.mark.parametrize("count", [True, -1, 1.5, "2"])
def test_reinforcement_rejects_invalid_counts(count):
    with pytest.raises(ValueError):
        reinforced_stability(1.0, count)


def test_reinforcement_interaction_order():
    results = {
        name: reinforced_stability(1.0, 0, boost=value)[0]
        for name, value in scoring.INTERACTION_BOOST.items()
    }
    assert results["create"] > results["reply"] > results["engage"]
    assert results["engage"] > results["recall"] > results["view"]


def test_reinforcement_caps_state_and_event_counter():
    stability, count = reinforced_stability(
        1e300, MAX_ACCESS_COUNT, boost=scoring.INTERACTION_BOOST["create"]
    )
    assert stability == MAX_STABILITY_DAYS
    assert count == MAX_ACCESS_COUNT
    assert math.isfinite(stability)
