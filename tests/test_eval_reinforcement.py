import pytest

from eval.reinforcement import _gain_checks, run


@pytest.mark.parametrize(
    ("gains", "failed_check"),
    [
        ([1.0, 0.5, 0.6], "nonincreasing"),
        ([1.0, float("nan"), 0.1], "finite"),
        ([1.0, -0.1, -0.2], "nonnegative"),
    ],
)
def test_reinforcement_gate_rejects_invalid_full_trajectories(gains, failed_check):
    checks = _gain_checks(gains)

    assert checks[failed_check] is False


def test_production_reinforcement_trajectory_passes_full_gate():
    report = run()

    assert report["passed"] is True
    assert all(report["checks"].values())
