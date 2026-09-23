from pathlib import Path

import pytest

from eval.user_journeys import run_journeys
from scripts import export_user_journey_evidence as exporter


@pytest.mark.parametrize("changed", [None, "dataset", "producer"])
def test_journey_export_binds_the_completed_envelope_to_evaluated_sources(tmp_path, monkeypatch, changed):
    source_root = tmp_path / "source"
    script_path = source_root / "scripts/export.py"
    paths = [source_root / name for name in (
        "engraphis/core.py", "eval/user_journeys.py", "eval/benchmark.py")]
    paths.append(script_path)
    paths.extend(
        source_root / "engraphis" / f"pkg{index}" / "__init__.py"
        for index in range(11)
    )
    paths.extend([
        source_root / "engraphis" / "core" / "query_planner.py",
        source_root / "engraphis" / "legacy" / "query_planner.py",
        source_root / "engraphis" / "core" / "recall.py",
        source_root / "engraphis" / "legacy" / "recall.py",
    ])
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# {path.relative_to(source_root).as_posix()}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(exporter, "ROOT", source_root)
    monkeypatch.setattr(exporter, "__file__", str(script_path))
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
        report = exporter.export(output)
        names = [item["name"] for item in report["suite"]["sources"]]
        assert report["metrics"]["source_stable"] is True
        assert len(names) == len(set(names))
        assert sum(name.endswith("/__init__.py") for name in names) == 11
        assert "engraphis/core/query_planner.py" in names
        assert "engraphis/legacy/query_planner.py" in names
        assert "engraphis/core/recall.py" in names
        assert "engraphis/legacy/recall.py" in names
        assert all(not Path(name).is_absolute() for name in names)
        assert all("\\" not in name for name in names)
        expected_hashes = {
            path.relative_to(source_root).as_posix(): exporter.sha256_file(path)
            for path in paths
        }
        assert {
            item["name"]: item["sha256"]
            for item in report["suite"]["sources"]
        } == expected_hashes
        assert output.exists() and output.with_suffix(".json.sha256").exists()


def test_source_label_rejects_paths_outside_repository_root(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("# outside\n", encoding="utf-8")
    monkeypatch.setattr(exporter, "ROOT", source_root)

    with pytest.raises(ValueError, match="outside the repository root"):
        exporter._source_label(outside)


def test_source_label_rejects_symlink_escape_when_supported(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    target = tmp_path / "outside.py"
    link = source_root / "engraphis" / "escape.py"
    source_root.mkdir()
    target.write_text("# outside\n", encoding="utf-8")
    link.parent.mkdir()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable on this Windows test host")
    monkeypatch.setattr(exporter, "ROOT", source_root)

    with pytest.raises(ValueError, match="outside the repository root"):
        exporter._source_label(link)


def test_producer_sources_reject_duplicate_resolved_labels_when_supported(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    core = source_root / "engraphis/core.py"
    alias = source_root / "engraphis/alias.py"
    core.parent.mkdir(parents=True)
    core.write_text("# producer\n", encoding="utf-8")
    try:
        alias.symlink_to(core)
    except OSError:
        pytest.skip("symlinks unavailable on this Windows test host")
    monkeypatch.setattr(exporter, "ROOT", source_root)
    monkeypatch.setattr(exporter, "__file__", str(source_root / "scripts/export.py"))

    with pytest.raises(ValueError, match="source labels are not unique"):
        exporter._producer_sources()


def test_journey_export_rejects_new_producer_added_during_execution(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    script_path = source_root / "scripts/export.py"
    paths = [source_root / name for name in (
        "engraphis/core.py", "eval/user_journeys.py", "eval/benchmark.py")]
    paths.append(script_path)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original source\n", encoding="utf-8")
    monkeypatch.setattr(exporter, "ROOT", source_root)
    monkeypatch.setattr(exporter, "__file__", str(script_path))
    observed = run_journeys(["index_repair"])
    new_source = source_root / "engraphis/new_module.py"

    def add_source_during_run():
        new_source.write_text("# added producer\n", encoding="utf-8")
        return observed

    monkeypatch.setattr(exporter, "run_journeys", add_source_during_run)
    with pytest.raises(ValueError, match="source changed during execution"):
        exporter.export(tmp_path / "journeys.json")
