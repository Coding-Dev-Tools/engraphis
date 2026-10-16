"""Spec Crawl CI checks each advertised metric as well as the aggregate score."""
import json
import math
from collections import Counter

import pytest

from eval import spec_crawl


METRICS = (
    "determinism_rate", "axes_accuracy", "flag_recall", "qualifier_accuracy",
    "sections_accuracy", "scores_accuracy",
)
FLOORS = [(metric, 1.0 if metric == "scores_accuracy" else 0.90) for metric in METRICS]
FLOORS.append(("overall_accuracy", 0.90))


def result(**overrides):
    values = {metric: 1.0 for metric in METRICS}
    values.update(overrides)
    values.setdefault("overall_accuracy", sum(values[metric] for metric in METRICS) / len(METRICS))
    return {"total_specs": 5, "agents_dogfood_ok": True, **values}


@pytest.mark.parametrize("metric", METRICS)
def test_one_metric_at_forty_percent_cannot_hide_in_a_passing_mean(tmp_path, monkeypatch, metric):
    # Real run() aggregation over a controlled five-record dataset. The crawler
    # wrapper introduces exactly one kind of regression into three records.
    texts = [f"# Rules\nVerify all tests for {name}." for name in ("alpha", "beta", "gamma", "delta", "epsilon")]
    dataset = tmp_path / "controlled.jsonl"
    dataset.write_text("\n".join(json.dumps({
        "text": text, "expected_sections": 1, "expected_axes": ["rules"],
        "expected_flags": ["all"], "unexpected_flags": ["phantom"],
        "min_score": 0, "max_score": 100,
    }) for text in texts) + "\n", encoding="utf-8")
    original = spec_crawl.crawl_spec
    calls = Counter()

    def regressed(text):
        report = original(text)
        if text not in texts[:3]:
            return report  # The actual AGENTS.md dogfood stays untouched.
        calls[text] += 1
        if metric == "determinism_rate" and calls[text] % 2 == 0:
            report["trace_sha256"] = "different-on-repeat"
        elif metric == "axes_accuracy":
            report["sections"] = [{**section, "axis": "other"} for section in report["sections"]]
        elif metric == "flag_recall":
            report["flags"] = []
        elif metric == "qualifier_accuracy":
            report["flags"].append({"token": "phantom"})
        elif metric == "sections_accuracy":
            report["sections"].append({"axis": "other"})
        elif metric == "scores_accuracy":
            report["score"] = 101
        return report

    monkeypatch.setattr(spec_crawl, "DATASET_PATH", dataset)
    monkeypatch.setattr(spec_crawl, "crawl_spec", regressed)
    measured = spec_crawl.run()
    assert measured[metric] == 0.40
    assert measured["overall_accuracy"] == pytest.approx(0.90)
    assert all(measured[other] == 1.0 for other in METRICS if other != metric)
    assert measured["agents_dogfood_ok"] is True
    calls.clear()
    with pytest.raises(SystemExit) as exc:
        spec_crawl.main()
    assert exc.value.code == 1


@pytest.mark.parametrize("metric,floor", FLOORS)
def test_each_metric_accepts_its_inclusive_floor(monkeypatch, metric, floor):
    measured = result(**{metric: floor})
    monkeypatch.setattr(spec_crawl, "run", lambda: measured)
    spec_crawl.main()


@pytest.mark.parametrize("metric,floor", FLOORS)
def test_each_metric_rejects_the_first_float_below_its_floor(monkeypatch, capsys, metric, floor):
    measured = result(**{metric: math.nextafter(floor, 0.0)})
    monkeypatch.setattr(spec_crawl, "run", lambda: measured)
    with pytest.raises(SystemExit) as exc:
        spec_crawl.main()
    assert exc.value.code == 1
    assert "FLOOR VIOLATION" in capsys.readouterr().err


def test_all_individual_floors_can_pass_together(monkeypatch):
    measured = result(**{metric: floor for metric, floor in FLOORS if metric != "overall_accuracy"})
    assert measured["overall_accuracy"] > 0.90
    monkeypatch.setattr(spec_crawl, "run", lambda: measured)
    spec_crawl.main()


def test_dogfood_requirement_is_preserved(monkeypatch):
    measured = {**result(), "agents_dogfood_ok": False}
    monkeypatch.setattr(spec_crawl, "run", lambda: measured)
    with pytest.raises(SystemExit) as exc:
        spec_crawl.main()
    assert exc.value.code == 1


def test_real_spec_fixtures_and_agents_dogfood_still_pass():
    measured = spec_crawl.run()
    assert measured["total_specs"] > 0
    assert measured["agents_dogfood_ok"] is True
    assert all(measured[metric] >= floor for metric, floor in FLOORS)
