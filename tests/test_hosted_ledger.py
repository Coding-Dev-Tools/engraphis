"""Offline privacy and resume contracts for the hosted benchmark checkpoint ledger."""
from __future__ import annotations

import json

import pytest

from eval.hosted_ledger import (
    MAX_NORMALIZED_ANSWER_CHARS,
    AttemptIdentity,
    CheckpointTurn,
    HostedLedgerError,
    PrivateHostedLedger,
    RunBinding,
    normalize_answer,
    resolve_private_ledger_path,
    text_sha256,
)


def _binding():
    return RunBinding(
        model="gpt-5.6-luna",
        dataset_sha256=text_sha256("dataset"),
        config_sha256=text_sha256("config"),
        repo_revision="a" * 40,
        repo_dirty=True,
        repo_dirty_sha256=text_sha256("dirty state"),
    )


def _identity(turn=0):
    return AttemptIdentity(
        repetition=2, strategy="adaptive", task_ordinal=7, turn_ordinal=turn,
    )


def test_repo_path_is_limited_to_private_eval_and_external_path_is_absolute(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    allowed = resolve_private_ledger_path(".private-eval/run.jsonl", repo_root=repo)
    assert allowed == (repo / ".private-eval" / "run.jsonl").resolve()
    with pytest.raises(HostedLedgerError, match=".private-eval"):
        resolve_private_ledger_path("runs/run.jsonl", repo_root=repo)
    with pytest.raises(HostedLedgerError, match="absolute"):
        resolve_private_ledger_path("../outside.jsonl", repo_root=repo)
    external = (tmp_path / "external.jsonl").resolve()
    assert resolve_private_ledger_path(external, repo_root=repo) == external


def test_repo_local_test_temp_path_is_allowed_only_under_an_ignored_tmp_directory(tmp_path):
    """A repo-local pytest base temp can safely host private test records.

    The system temp directory is not always writable on locked-down Windows hosts.  The
    exception is deliberately narrower than a generic repo-local path and remains ignored.
    """

    repo = tmp_path / "repo"
    repo.mkdir()
    path = resolve_private_ledger_path(".tmp-pytest/private/records.jsonl", repo_root=repo)

    assert path == (repo / ".tmp-pytest" / "private" / "records.jsonl").resolve()
    with pytest.raises(HostedLedgerError, match=".private-eval"):
        resolve_private_ledger_path(".scratch/private/records.jsonl", repo_root=repo)


def test_completed_record_is_prompt_free_bound_and_resumable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "run.jsonl"
    ledger = PrivateHostedLedger(path, _binding(), repo_root=repo)
    key = _identity()
    assert ledger.reserve_call(key, max_calls=2) == 1
    ledger.append_completed(key, CheckpointTurn(answer="  Ada\nLovelace  ", input_tokens=11))
    ledger.close()

    replayed = PrivateHostedLedger(path, _binding(), repo_root=repo)
    assert replayed.calls_started == 1
    assert replayed.resume(key) == CheckpointTurn(answer="Ada Lovelace", input_tokens=11)
    raw = path.read_text(encoding="utf-8")
    assert "prompt" not in raw and "context" not in raw and "question" not in raw
    record = json.loads(raw.splitlines()[1])
    assert record["attempt_key"] == "2:adaptive:7:0"
    assert record["dataset_sha256"] == _binding().dataset_sha256
    replayed.close()


def test_binding_and_duplicate_completed_attempts_fail_closed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "run.jsonl"
    ledger = PrivateHostedLedger(path, _binding(), repo_root=repo)
    ledger.reserve_call(_identity(), max_calls=2)
    ledger.append_completed(_identity(), CheckpointTurn(answer="Ada"))
    with pytest.raises(HostedLedgerError, match="already contains"):
        ledger.append_completed(_identity(), CheckpointTurn(answer="Ada"))
    ledger.close()
    other = RunBinding(
        model="gpt-5.6-luna", dataset_sha256=text_sha256("other"),
        config_sha256=_binding().config_sha256, repo_revision="a" * 40,
        repo_dirty=True, repo_dirty_sha256=_binding().repo_dirty_sha256,
    )
    with pytest.raises(HostedLedgerError, match="another benchmark binding"):
        PrivateHostedLedger(path, other, repo_root=repo)


def test_events_persist_retries_failures_and_global_call_ceiling_across_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "run.jsonl"
    ledger = PrivateHostedLedger(path, _binding(), repo_root=repo)
    ledger.reserve_call(_identity(), max_calls=2)
    ledger.append_retry(_identity(), error_class="transport_timeout")
    ledger.reserve_call(_identity(turn=1), max_calls=2)
    ledger.append_failure(_identity(turn=1), error_class="rate_limited")
    ledger.close()
    replayed = PrivateHostedLedger(path, _binding(), repo_root=repo)
    assert replayed.calls_started == 2
    with pytest.raises(HostedLedgerError, match="ceiling"):
        replayed.reserve_call(_identity(turn=2), max_calls=2)
    kinds = [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert kinds == ["call_started", "retry", "call_started", "failure"]
    replayed.close()


def test_terminal_and_interrupted_attempts_cannot_gain_calls_after_restart(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    failed_path = repo / ".private-eval" / "failed.jsonl"
    failed = PrivateHostedLedger(failed_path, _binding(), repo_root=repo)
    failed.reserve_call(_identity(), max_calls=3)
    failed.append_failure(_identity(), error_class="runtime")
    failed.close()

    replayed_failure = PrivateHostedLedger(failed_path, _binding(), repo_root=repo)
    with pytest.raises(HostedLedgerError, match="terminal"):
        replayed_failure.reserve_call(_identity(), max_calls=3)
    replayed_failure.close()

    interrupted_path = repo / ".private-eval" / "interrupted.jsonl"
    interrupted = PrivateHostedLedger(interrupted_path, _binding(), repo_root=repo)
    interrupted.reserve_call(_identity(), max_calls=3)
    interrupted.close()

    replayed_interrupted = PrivateHostedLedger(
        interrupted_path, _binding(), repo_root=repo,
    )
    with pytest.raises(HostedLedgerError, match="interrupted"):
        replayed_interrupted.reserve_call(_identity(), max_calls=3)
    replayed_interrupted.close()


def test_retry_event_allows_only_one_following_reservation(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "retry.jsonl"
    ledger = PrivateHostedLedger(path, _binding(), repo_root=repo)
    ledger.reserve_call(_identity(), max_calls=3)
    ledger.append_retry(_identity(), error_class="transport")
    assert ledger.reserve_call(_identity(), max_calls=3) == 2
    with pytest.raises(HostedLedgerError, match="interrupted"):
        ledger.reserve_call(_identity(), max_calls=3)
    ledger.close()


def test_rejects_oversized_or_non_normalized_answers_and_duplicate_records(tmp_path):
    with pytest.raises(HostedLedgerError, match="size cap"):
        normalize_answer("x" * (MAX_NORMALIZED_ANSWER_CHARS + 1))
    with pytest.raises(HostedLedgerError, match="string"):
        normalize_answer(None)

    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "run.jsonl"
    ledger = PrivateHostedLedger(path, _binding(), repo_root=repo)
    ledger.reserve_call(_identity(), max_calls=2)
    ledger.append_completed(_identity(), CheckpointTurn(answer="Ada"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(path.read_text(encoding="utf-8").splitlines()[-1] + "\n")
    ledger.close()
    with pytest.raises(HostedLedgerError, match="duplicate"):
        PrivateHostedLedger(path, _binding(), repo_root=repo)


def test_private_ledger_has_an_exclusive_process_lock(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    path = repo / ".private-eval" / "run.jsonl"
    first = PrivateHostedLedger(path, _binding(), repo_root=repo)
    with pytest.raises(HostedLedgerError, match="already holds"):
        PrivateHostedLedger(path, _binding(), repo_root=repo)
    first.close()
    second = PrivateHostedLedger(path, _binding(), repo_root=repo)
    second.close()
