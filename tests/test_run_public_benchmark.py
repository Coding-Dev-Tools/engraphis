"""Focused contracts for the locked public benchmark orchestrator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts import run_public_benchmark as runner


COMMIT = "a" * 40
DATASET_REVISION = "b" * 40
SOURCE_REVISION = "c" * 40
EMBED_REVISION = "d" * 40
READER_REVISION = "e" * 40


def manifest(tmp_path: Path, *, runner_name: str = "harness") -> dict:
    dataset = tmp_path / "dataset.jsonl"
    dataset.write_text('{"id":"q1"}\n', encoding="utf-8")
    profile = tmp_path / "profile.json"
    profile.write_text(
        json.dumps({
            "benchmark": {
                "repository": "example/benchmark",
                "repository_revision": SOURCE_REVISION,
                "dataset_revision": DATASET_REVISION,
            },
            "embedding": {"model": "example/embed", "revision": EMBED_REVISION},
            "reader": {"model": "example/reader", "revision": READER_REVISION},
            "baseline_label": "full_hybrid",
            "token_budgets": runner.CANONICAL_BUDGETS,
        }),
        encoding="utf-8",
    )
    return {
        "schema": runner.SCHEMA,
        "locked": True,
        "run_id": "test-run-001",
        "runner": runner_name,
        "benchmark": {"name": "fixture", "format": "jsonl" if runner_name == "harness" else "locomo"},
        "dataset": {"path": str(dataset), "sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "revision": DATASET_REVISION},
        "source": {"repository": "example/benchmark", "revision": SOURCE_REVISION},
        "models": {
            "embedding": {"model": "example/embed", "revision": EMBED_REVISION},
            "reader": {"model": "example/reader", "revision": READER_REVISION},
        },
        "repo": {"root": str(tmp_path), "commit": COMMIT},
        "config": {"baseline_label": "full_hybrid", "token_budgets": runner.CANONICAL_BUDGETS, "k": 10, "canonical_profile": "profile.json"},
        "outputs": {"directory": "artifacts", "report": "report.json", "artifact": "public.json", "claims": "claims.json"},
    }


def test_manifest_requires_lock_and_immutable_provenance(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    value["locked"] = False
    with pytest.raises(runner.ManifestError, match="locked must be true"):
        runner.validate_manifest(value)

    value = manifest(tmp_path)
    value["models"]["embedding"]["revision"] = "main"
    with pytest.raises(runner.ManifestError, match="lowercase 40-character commit"):
        runner.validate_manifest(value)


def test_manifest_rejects_unknown_and_unsafe_fields(tmp_path: Path) -> None:
    value = manifest(tmp_path)
    value["command"] = ["curl", "https://example.test"]
    with pytest.raises(runner.ManifestError, match="unknown manifest fields"):
        runner.validate_manifest(value)

    value = manifest(tmp_path)
    value["outputs"]["artifact"] = "../published.json"
    with pytest.raises(runner.ManifestError, match="relative output path"):
        runner.validate_manifest(value)


def test_plan_has_canonical_runner_artifact_and_claim_validation(tmp_path: Path) -> None:
    plan = runner.build_plan(manifest(tmp_path))
    assert plan["execute_required"] is True
    assert plan["network_policy"] == "offline_assets_only"
    assert [item["kind"] for item in plan["commands"]] == ["benchmark", "claim_validation"]
    assert plan["commands"][-1]["command"][2:5] == ["eval.public_readiness", "--artifact", plan["outputs"]["artifact"]]
    assert "<locked-dataset>" in plan["commands"][0]["redacted_command"]
    assert "<public-artifact>" in plan["commands"][-1]["redacted_command"]
    assert "download" not in json.dumps(plan).lower()


def test_runner_rejects_the_diagnostic_external_adapter(tmp_path: Path) -> None:
    with pytest.raises(runner.ManifestError, match="runner must be one of: harness"):
        runner.build_plan(manifest(tmp_path, runner_name="external"))


def test_execute_requires_local_hash_and_runs_only_with_explicit_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = manifest(tmp_path)
    claims_input = tmp_path / "reviewed-claims.json"
    claims_input.write_text("[]\n", encoding="utf-8")
    calls: list[tuple[list[str], dict]] = []
    def check_output(command, *args, **kwargs):
        return "" if "status" in command else COMMIT + "\n"

    monkeypatch.setattr(runner.subprocess, "check_output", check_output)

    def fake_run(command: list[str], **kwargs: object) -> None:
        if command[2] == "eval.public_readiness":
            assert Path(command[-1]).read_text(encoding="utf-8") == "[]\n"
        calls.append((command, kwargs))

    plan = runner.build_plan(value)
    runner.execute_plan(plan, value, claims_input=claims_input, runner=fake_run)
    assert len(calls) == 2
    assert calls[0][1]["check"] is True
    assert calls[0][1]["env"]["HF_HUB_OFFLINE"] == "1"

    value["dataset"]["sha256"] = "f" * 64
    with pytest.raises(runner.ManifestError, match="does not match local dataset"):
        runner.execute_plan(plan, value, claims_input=claims_input, runner=fake_run)

    altered = dict(plan)
    altered["commands"] = []
    with pytest.raises(runner.ManifestError, match="plan commands do not match"):
        runner.execute_plan(altered, manifest(tmp_path), claims_input=claims_input, runner=fake_run)


def test_claims_input_is_required_and_cannot_replace_staged_claims(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    value = manifest(tmp_path)
    claims_input = tmp_path / "reviewed-claims.json"
    claims_input.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kwargs: "" if "status" in command else COMMIT + "\n",
    )
    plan = runner.build_plan(value)

    with pytest.raises(runner.ManifestError, match="claims_input is required"):
        runner.execute_plan(plan, value)

    claims_output = Path(plan["outputs"]["claims"])
    claims_output.parent.mkdir(parents=True)
    claims_output.write_text('[{"text":"different"}]\n', encoding="utf-8")
    with pytest.raises(runner.ManifestError, match="refusing to replace staged claims"):
        runner.execute_plan(plan, value, claims_input=claims_input)


def test_execute_rejects_profile_drift_and_dirty_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = manifest(tmp_path)
    profile = tmp_path / "profile.json"
    profile_value = json.loads(profile.read_text(encoding="utf-8"))
    profile_value["reader"]["revision"] = "f" * 40
    profile.write_text(json.dumps(profile_value), encoding="utf-8")
    monkeypatch.setattr(
        runner.subprocess,
        "check_output",
        lambda command, **kwargs: "dirty\n" if "status" in command else COMMIT + "\n",
    )
    with pytest.raises(runner.ManifestError, match="reader.revision"):
        runner.execute_plan(runner.build_plan(value), value)

    profile_value["reader"]["revision"] = READER_REVISION
    profile.write_text(json.dumps(profile_value), encoding="utf-8")
    with pytest.raises(runner.ManifestError, match="clean worktree"):
        runner.execute_plan(runner.build_plan(value), value)


def test_main_is_dry_run_by_default(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest(tmp_path)), encoding="utf-8")
    assert runner.main(["--manifest", str(path)]) == 0
    captured = capsys.readouterr()
    assert "dry-run only" in captured.err
    assert '"execute_required": true' in captured.out
