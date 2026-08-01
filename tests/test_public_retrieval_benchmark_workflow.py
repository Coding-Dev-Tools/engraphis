from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "public-retrieval-benchmarks.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_retrieval_workflow_is_manual_protected_and_pinned() -> None:
    text = _workflow()

    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "pull_request:" not in text
    assert "schedule:" not in text
    assert "environment: public-benchmark-protected" in text
    assert "runs-on: [self-hosted, benchmark]" in text
    assert "manifest_path:" in text
    assert "series_path:" in text
    assert "environment_lock_path:" in text
    assert "environment_lock_sha256:" in text
    assert "claims_path:" in text
    assert "execution_authorized:" in text
    assert "locked comparison-series contract" in text
    assert "Validate the declared comparative series contract" in text
    assert 'test "$EXECUTION_AUTHORIZED" = "true"' in text
    assert "timeout-minutes: 1440" in text
    assert "git status --porcelain=v1 --untracked-files=all" in text


def test_retrieval_workflow_plans_before_execute_and_validates_the_series() -> None:
    text = _workflow()

    series = text.index("-m eval.public_readiness --series")
    dry_run = text.index("-m scripts.run_public_benchmark --manifest \"$MANIFEST_PATH\" \\")
    execute = text.index('--execute --claims-input "$CLAIMS_PATH"')
    upload = text.index("actions/upload-artifact@")
    assert series < dry_run < execute < upload
    assert "--plan-output \"$BENCHMARK_STATE_DIR/plan.json\"" in text
    assert "/opt/engraphis-benchmarks/manifests" in text
    assert "/opt/engraphis-benchmarks/series" in text
    assert "/opt/engraphis-benchmarks/claims" in text
    assert '--execute --claims-input "$CLAIMS_PATH"' in text


def test_retrieval_workflow_binds_immutable_inputs_and_offline_environment() -> None:
    text = _workflow()

    assert "actions/setup-python@" not in text
    assert "pip install" not in text
    assert "pip freeze --all --exclude-editable" in text
    assert "pip check" in text
    assert "sha256sum -c -" in text
    assert '[[ "$resolved" == "$root_real/"* ]]' in text
    assert 'point_root.resolve() != workspace' in text
    assert 'point["run_id"] != os.environ["RUN_ID"]' in text
    assert 'point["repo"]["commit"] != os.environ["GITHUB_SHA"]' in text
    assert 'series["source"]["git_commit"] != os.environ["GITHUB_SHA"]' in text
    assert 'output_root.resolve() != state' in text


def test_retrieval_workflow_exports_no_private_state() -> None:
    text = _workflow()
    upload_section = text[text.index("- name: Upload redacted public artifacts only") :]

    assert "${{ env.PUBLIC_ARTIFACT_DIR }}/${{ inputs.run_id }}.json" in upload_section
    assert "${{ env.PUBLIC_ARTIFACT_DIR }}/${{ inputs.run_id }}.claims.json" in upload_section
    assert "${{ env.PUBLIC_ARTIFACT_DIR }}/SHA256SUMS" in upload_section
    assert "BENCHMARK_STATE_DIR" not in upload_section
    assert "secrets." not in text
    assert "gh release" not in text
    assert "pypa/gh-action-pypi-publish" not in text
    export_section = text[
        text.index("- name: Export validated redacted artifacts only") :
        text.index("- name: Upload redacted public artifacts only")
    ]
    assert "source.is_symlink()" in export_section
    assert "state not in resolved.parents" in export_section
    assert "shutil.copy2(resolved, target)" in export_section
    assert "mkdir -p -- \"$state_dir\" \"$public_dir\"" in text
    assert "mkdir -p -- \"$state_dir\" public-artifacts" not in text
