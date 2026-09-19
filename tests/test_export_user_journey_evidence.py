import pytest

from eval.user_journeys import run_journeys
from scripts import export_user_journey_evidence as exporter


@pytest.mark.parametrize("changed", [None, "dataset", "producer"])
def test_journey_export_binds_the_completed_envelope_to_evaluated_sources(tmp_path, monkeypatch, changed):
    source_root = tmp_path / "source"
    paths = [source_root / name for name in (
        "engraphis/core.py", "eval/user_journeys.py", "eval/benchmark.py", "scripts/export.py")]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original source\n", encoding="utf-8")
    monkeypatch.setattr(exporter, "ROOT", source_root)
    monkeypatch.setattr(exporter, "__file__", str(paths[-1]))
    observed = run_journeys(["index_repair"])
    monkeypatch.setattr(exporter, "run_journeys", lambda: observed)
    original = exporter.report_envelope

    def mutate_after_validation(**kwargs):
        if changed:
            paths[1 if changed == "dataset" else 0].write_text("# changed source\n", encoding="utf-8")
        return original(**kwargs)

    monkeypatch.setattr(exporter, "report_envelope", mutate_after_validation)
    output = tmp_path / "journeys.json"
    if changed:
        with pytest.raises(ValueError, match="evaluated source snapshot"):
            exporter.export(output)
        assert not output.exists()
        assert not output.with_suffix(".json.sha256").exists()
    else:
        assert exporter.export(output)["metrics"]["source_stable"] is True
        assert output.exists() and output.with_suffix(".json.sha256").exists()
