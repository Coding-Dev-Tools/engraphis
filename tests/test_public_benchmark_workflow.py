from __future__ import annotations

from pathlib import Path


WORKFLOW = Path(__file__).parents[1] / ".github" / "workflows" / "public-benchmarks.yml"


def _workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_public_benchmark_workflow_is_manual_and_protected() -> None:
    text = _workflow()

    assert "workflow_dispatch:" in text
    assert "push:" not in text
    assert "pull_request:" not in text
    assert "schedule:" not in text
    assert "required: true\n        type: choice" in text
    assert "run_id:" in text
    assert "max_hosted_calls:" in text
    assert "prerequisites_reviewed:" in text
    assert "environment: public-benchmark-protected" in text
    assert "runs-on: [self-hosted, benchmark]" in text
    assert "permissions:\n  contents: read" in text


def test_public_benchmark_workflow_dry_runs_before_execution_and_validates() -> None:
    text = _workflow()

    dry_run = text.index("python -m eval.hosted_luna --dry-run --full")
    execute = text.index("python -m eval.hosted_luna --full")
    readiness = text.index("python -m eval.public_readiness")
    upload = text.index("actions/upload-artifact@")
    assert dry_run < execute < readiness < upload
    assert "projected_max_hosted_calls" in text
    assert "operator ceiling does not exactly match the frozen dry-run" in text
    assert "--max-hosted-calls \"$max_calls\"" in text


def test_public_benchmark_workflow_uses_safe_persistent_state() -> None:
    text = _workflow()

    assert "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" in text
    assert "ENGRAPHIS_BENCHMARK_STATE_ROOT" in text
    assert "benchmark state must be outside the checkout" in text
    assert '--private-records "$BENCHMARK_STATE_DIR/records.jsonl"' in text
    assert '--public-report "$BENCHMARK_STATE_DIR/public.json"' in text
    assert "git status --porcelain=v1 --untracked-files=all" in text


def test_public_benchmark_workflow_cannot_upload_private_run_material() -> None:
    text = _workflow()
    upload_section = text[text.index("- name: Upload redacted public artifacts only") :]

    assert "path: public-artifacts/" in upload_section
    assert ".private-eval" not in upload_section
    assert ".hosted-eval-results" not in upload_section
    assert "secrets." not in text
    assert "gh release" not in text
    assert "pypa/gh-action-pypi-publish" not in text
