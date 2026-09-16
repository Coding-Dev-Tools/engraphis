import copy

import pytest

from eval.benchmark import canonical_json, sha256_text
from scripts.render_coding_campaign_chart import chart_data


def inputs():
    manifest = {"stages": {"pilot": {"scenario_ids": ["a", "b", "c", "d"], "arms": ["hybrid"],
                                     "token_budgets": [512], "repetitions": 1}}}
    manifest["binding_sha256"] = sha256_text(canonical_json(manifest))
    rows = [{"scenario_id": identity, "arm": "hybrid", "token_budget": 512, "repetition": 0,
             "family_id": "one", "status": status, "task_success": success,
             "critical_violation_count": 0}
            for identity, status, success in (("a", "complete", True), ("b", "complete", False),
                                              ("c", "unsupported", None))]
    report = {"records": rows, "metrics": {"stage": "pilot", "campaign_sha256": manifest["binding_sha256"],
              "expected_attempts": 4, "missing_attempts": 1, "critical_violations": 0,
              "statuses": {"complete": 2, "unsupported": 1},
              "arms": {"hybrid": {"attempts": 3, "complete": 2, "successes": 1}}}}
    return report, manifest


def test_chart_keeps_unsupported_and_missing_out_of_quality_failures():
    report, manifest = inputs()
    data = chart_data(report, manifest)
    assert data["panels"][0]["arms"][0]["counts"] == {
        "success": 1, "task_failure": 1, "unsupported": 1, "missing": 1,
    }
    assert data["families"] == 1


@pytest.mark.parametrize("field,value", [("missing_attempts", 0), ("expected_attempts", 3),
                                         ("critical_violations", 1)])
def test_chart_rejects_summary_count_drift(field, value):
    report, manifest = inputs()
    report["metrics"][field] = value
    with pytest.raises(ValueError, match="counts differ"):
        chart_data(report, manifest)


def test_chart_rejects_duplicate_attempt_or_false_unscored_failure():
    report, manifest = inputs()
    duplicate = copy.deepcopy(report)
    duplicate["records"].append(duplicate["records"][0])
    with pytest.raises(ValueError, match="duplicate"):
        chart_data(duplicate, manifest)
    report["records"][2]["task_success"] = False
    with pytest.raises(ValueError, match="unscored"):
        chart_data(report, manifest)


def test_chart_rejects_manifest_and_aggregate_drift():
    report, manifest = inputs()
    report["metrics"]["arms"]["hybrid"]["successes"] = 3
    with pytest.raises(ValueError, match="aggregate"):
        chart_data(report, manifest)
    manifest["stages"]["pilot"]["repetitions"] = 2
    with pytest.raises(ValueError, match="binding"):
        chart_data(report, manifest)


def test_chart_separates_invalid_fixture_from_task_failures():
    report, manifest = inputs()
    excluded = {key: report["records"][1][key] for key in ("scenario_id", "arm", "token_budget", "repetition")}
    excluded["reason"] = "invalid_fixture_exact_wording"
    mask = {"schema": "engraphis-campaign-eligibility/v1", "origin": "implementation_team",
            "parent_campaign_sha256": manifest["binding_sha256"], "stage": "pilot",
            "retrospective": True, "exclusions": [excluded]}
    mask["binding_sha256"] = sha256_text(canonical_json(mask))
    data = chart_data(report, manifest, mask)
    assert data["panels"][0]["arms"][0]["counts"] == {
        "success": 1, "invalid_fixture": 1, "unsupported": 1, "missing": 1,
    }
    assert data["excluded"] == 1 and data["valid_missing"] == 1
    manifest["stages"]["pilot"]["token_budgets"].append(1500)
    manifest["binding_sha256"] = sha256_text(canonical_json(
        {key: value for key, value in manifest.items() if key != "binding_sha256"}))
    report["metrics"]["campaign_sha256"] = manifest["binding_sha256"]
    mask["parent_campaign_sha256"] = manifest["binding_sha256"]
    mask["binding_sha256"] = sha256_text(canonical_json(
        {key: value for key, value in mask.items() if key != "binding_sha256"}))
    with pytest.raises(ValueError, match="whole scenarios"):
        chart_data(report, manifest, mask)
