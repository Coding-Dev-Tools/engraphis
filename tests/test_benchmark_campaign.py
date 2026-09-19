import json
from types import SimpleNamespace

import pytest

from eval import benchmark_campaign as campaign
from eval.benchmark import canonical_json, sha256_file, validate_report


def small_manifest():
    manifest = {"stages": {"development_pilot": {
        "split": "development", "scenario_ids": ["fixture-a"], "arms": ["no_memory", "hybrid"],
        "repetitions": 1, "token_budgets": [512], "max_reader_turns": 2,
        "max_peer_internal_calls_per_attempt": 32, "max_input_tokens": 32768, "max_output_tokens": 4096,
    }}}
    manifest["binding_sha256"] = campaign.digest(manifest)
    return manifest


def row(cell, **extra):
    return {**cell, "family_id": "family-a", "category": "corrections", "status": "complete",
            "task_success": True, "critical_violations": [], "private_responses": ["SECRET ANSWER"], **extra}


def test_interrupted_reservation_is_not_replayed(tmp_path):
    manifest = small_manifest()
    cell = campaign.cells(manifest, "development_pilot")[0]
    path = tmp_path / "development_pilot" / (campaign.digest(cell) + ".started")
    path.parent.mkdir()
    path.write_text("{}")
    with pytest.raises(ValueError, match="unfinished attempt"):
        campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                         attempt_runner=lambda *args: pytest.fail("duplicate dispatch"))


def test_completed_attempt_resumes_without_duplicate(tmp_path):
    manifest = small_manifest()
    calls = []

    def runner(_manifest, _stage, cell, _corpus, _client):
        calls.append(cell)
        return row(cell)

    partial = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               maximum_attempts=1, attempt_runner=runner)
    assert partial["status"] == "PARTIAL"
    assert partial["missing_attempts"] == 1
    complete = campaign.execute(manifest, "development_pilot", tmp_path, None, None, attempt_runner=runner)
    assert complete["status"] == "COMPLETE"
    assert len(calls) == 2
    assert complete["noninferiority"] == "indeterminate"


def test_campaign_attempt_reaches_real_ledger_when_digest_starts_with_digit(tmp_path, monkeypatch):
    from eval.campaign_api import LunaResponsesClient, MODEL
    from eval.campaign_ledger import BudgetApproval, CampaignBinding, CampaignLedger
    from eval.coding_corpus import load_corpus

    corpus = load_corpus()
    scenario = corpus.scenarios("development")[0]
    manifest = small_manifest()
    manifest.update(source={}, docker_image="unused-isolated-oracle")
    cell = {"scenario_id": scenario.id, "arm": "no_memory", "token_budget": 512, "repetition": 0}
    monkeypatch.setattr(campaign, "digest", lambda value: "0" * 64)
    binding = CampaignBinding(campaign_id="attempt-label-regression", model=MODEL,
        reasoning_effort="medium", dataset_sha256="a" * 64, config_sha256="b" * 64,
        repo_revision="fixture", pins_sha256="c" * 64)
    ledger = CampaignLedger(tmp_path / "ledger.jsonl", binding,
        BudgetApproval.create(max_calls=1, max_cost_micros=20000))
    class Transport:
        def create(self, **kwargs):
            return {"model": MODEL, "output_text": json.dumps({"answer": "done", "citations": [], "files": {}}),
                    "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                              "input_tokens_details": {"cached_tokens": 0},
                              "output_tokens_details": {"reasoning_tokens": 0}}}
    result = campaign.run_attempt(manifest, "development_pilot", cell, corpus,
        LunaResponsesClient(ledger, transport=Transport()),
        oracle=lambda *args: {"passed": True, "timed_out": False})
    assert result["status"] == "complete"
    assert result["reader_calls"] == 1
    assert ledger.lookup("attempt-" + "0" * 32 + "-reader-0").status == "completed"


def test_failed_call_remains_visible_and_stops_resume(tmp_path):
    def runner(*args):
        raise RuntimeError("raw provider SECRET must not be published")

    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None, attempt_runner=runner)
    assert summary["statuses"] == {"error": 1}
    assert summary["missing_attempts"] == 1
    resumed = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                                attempt_runner=lambda *args: pytest.fail("terminal error replay"))
    assert resumed["statuses"] == {"error": 1}
    assert "SECRET" not in json.dumps(resumed)


def test_checkpoint_content_tampering_fails(tmp_path):
    manifest = small_manifest()
    campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                     maximum_attempts=1, attempt_runner=lambda m, s, c, *args: row(c))
    path = next((tmp_path / "development_pilot").glob("*.json"))
    content = json.loads(path.read_text())
    content["row"]["task_success"] = False
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError, match="checksum"):
        campaign.summarize(manifest, "development_pilot", tmp_path)


def test_public_boundary_omits_private_outputs(tmp_path):
    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c))
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest))
    public = campaign.public_report(path, summary)
    assert "SECRET ANSWER" not in json.dumps(public)
    assert public["records"][0]["arm"] == "no_memory"
    assert public["metrics"]["expected_attempts"] == 2
    assert public["metrics"]["leadership_eligible"] is False
    assert not validate_report(public)


@pytest.mark.parametrize("rehash", [False, True])
def test_public_report_rejects_manifest_changes_after_execution(tmp_path, rehash):
    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c))
    manifest["docker_image"] = "changed-after-execution"
    if rehash:
        manifest["binding_sha256"] = campaign.digest(
            {key: value for key, value in manifest.items() if key != "binding_sha256"})
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="evaluated snapshot"):
        campaign.public_report(path, summary)


def test_public_report_rejects_changed_raw_manifest_bytes(tmp_path):
    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c))
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest), encoding="utf-8")
    original_sha256 = sha256_file(path)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with pytest.raises(ValueError, match="evaluated snapshot"):
        campaign.public_report(path, summary, expected_manifest_sha256=original_sha256)


@pytest.mark.parametrize("changed", ["manifest", "producer"])
def test_public_report_rechecks_completed_envelope(tmp_path, monkeypatch, changed):
    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c))
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest), encoding="utf-8")
    producer = tmp_path / "producer.py"
    producer.write_text("# original producer\n", encoding="utf-8")
    monkeypatch.setattr(campaign, "__file__", str(producer))
    original = campaign.report_envelope

    def mutate(**kwargs):
        target = path if changed == "manifest" else producer
        target.write_text(target.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        return original(**kwargs)

    monkeypatch.setattr(campaign, "report_envelope", mutate)
    with pytest.raises(ValueError, match="artifact construction"):
        campaign.public_report(path, summary)


@pytest.mark.parametrize("phase", ["before_report", "during_envelope"])
def test_public_report_revalidates_all_frozen_producers(tmp_path, monkeypatch, phase):
    manifest = small_manifest()
    manifest["source"] = {"engraphis/core/context.py": "a" * 64}
    manifest["binding_sha256"] = campaign.digest(
        {key: value for key, value in manifest.items() if key != "binding_sha256"})
    observed = dict(manifest["source"])
    monkeypatch.setattr(campaign, "source_snapshot", lambda: dict(observed))
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c))
    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest), encoding="utf-8")
    if phase == "before_report":
        observed["engraphis/core/context.py"] = "b" * 64
    else:
        original = campaign.report_envelope

        def change_core(**kwargs):
            observed["engraphis/core/context.py"] = "b" * 64
            return original(**kwargs)

        monkeypatch.setattr(campaign, "report_envelope", change_core)
    with pytest.raises(ValueError, match="producer differs"):
        campaign.public_report(path, summary)


def test_campaign_rechecks_producers_after_final_summary(tmp_path, monkeypatch):
    manifest = small_manifest()
    manifest["source"] = {}
    observed = {}
    monkeypatch.setattr(campaign, "source_snapshot", lambda: dict(observed))
    original = campaign.summarize

    def mutate_after_attempts(*args, **kwargs):
        summary = original(*args, **kwargs)
        observed["changed.py"] = "b" * 64
        return summary

    monkeypatch.setattr(campaign, "summarize", mutate_after_attempts)
    with pytest.raises(ValueError, match="source changed"):
        campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                         attempt_runner=lambda m, s, c, *args: row(c))


@pytest.mark.parametrize("change", ["file", "loaded"])
def test_campaign_rejects_corpus_metadata_drift_before_dispatch(tmp_path, change):
    manifest = small_manifest()
    corpus = SimpleNamespace(root=tmp_path, manifest={"version": 1}, runtime={"operations": []})
    manifest["corpus"] = {}
    for name in ("manifest", "runtime"):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(getattr(corpus, name)), encoding="utf-8")
        manifest["corpus"][f"{name}_sha256"] = sha256_file(path)
    if change == "file":
        (tmp_path / "runtime.json").write_text("{}", encoding="utf-8")
    else:
        corpus.runtime = {"operations": ["different"]}
    with pytest.raises(ValueError, match="corpus bytes differ"):
        campaign.execute(manifest, "development_pilot", tmp_path, corpus, None,
                         attempt_runner=lambda *args: pytest.fail("dispatched changed corpus"))


def test_validation_receipt_hashes_the_checkpoint_bytes_actually_summarized(tmp_path, monkeypatch):
    manifest = small_manifest()
    manifest["stages"]["validation"] = manifest["stages"].pop("development_pilot")
    manifest.update(source={}, core_arms=["no_memory", "hybrid"])
    monkeypatch.setattr(campaign, "source_snapshot", lambda: {})
    campaign.execute(manifest, "validation", tmp_path, None, None,
                     attempt_runner=lambda m, s, c, *args: row(c))
    paths = list((tmp_path / "validation").glob("*.json"))
    expected = {path.name: sha256_file(path) for path in paths}
    original = campaign.summarize

    def mutate_after_parsing(*args, **kwargs):
        result = original(*args, **kwargs)
        paths[0].write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(campaign, "summarize", mutate_after_parsing)
    receipt = campaign.validation_selection(manifest, tmp_path)
    assert receipt["validation_checkpoints"] == expected
    assert sha256_file(paths[0]) != expected[paths[0].name]


def test_oracle_timeout_is_unscored_and_not_replayed(tmp_path):
    manifest = small_manifest()
    calls = []

    def runner(_manifest, _stage, cell, _corpus, _client):
        calls.append(cell)
        return row(
            cell,
            status="error",
            task_success=None,
            oracle_outcome="timeout_unknown",
            unscored_reason="oracle_timeout_unknown",
            oracle_calls=1,
            private_oracles=[{
                "passed": False,
                "returncode": None,
                "timed_out": True,
                "oracle_outcome": "timeout_unknown",
                "stdout": "",
                "stderr": "oracle timeout",
            }],
        )

    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=runner)
    assert summary["status"] == "BLOCKED"
    assert summary["statuses"] == {"error": 1}
    assert summary["oracle_summary"]["timeouts"] == 1
    campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                     attempt_runner=lambda *args: pytest.fail("unscored attempt replayed"))
    assert len(calls) == 1

    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest))
    public = campaign.public_report(path, summary)
    assert public["metrics"]["oracle_summary"]["unscored"] == 1
    assert all(record["status"] == "error" for record in public["records"])
    assert all(record["oracle_outcome"] == "timeout_unknown" for record in public["records"])
    assert "oracle timeout" not in json.dumps(public)
    assert not validate_report(public)


def test_zero_exit_value_mismatch_remains_a_scored_failure(tmp_path):
    manifest = small_manifest()

    def runner(_manifest, _stage, cell, _corpus, _client):
        return row(
            cell,
            task_success=False,
            oracle_outcome="value_mismatch",
            oracle_calls=1,
            private_oracles=[{
                "passed": False,
                "returncode": 0,
                "timed_out": False,
                "oracle_outcome": "value_mismatch",
                "stdout": "",
                "stderr": "",
            }],
        )

    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=runner)
    assert summary["status"] == "COMPLETE"
    assert summary["arms"]["no_memory"]["successes"] == 0
    assert summary["oracle_summary"]["value_mismatches"] == 2
    assert summary["oracle_summary"]["unscored"] == 0


def test_summary_and_public_report_preserve_safe_oauth_usage_totals(tmp_path):
    manifest = small_manifest()
    usage = [{
        "input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 5,
        "reasoning_output_tokens": 2, "total_tokens": 16, "latency_ms": 7.5,
        "cost_micros": 123, "transport_identity": "codex_oauth",
        "billing_basis": campaign.OAUTH_BILLING_BASIS,
    }]
    summary = campaign.execute(
        manifest, "development_pilot", tmp_path, None, None,
        attempt_runner=lambda m, s, c, *args: row(c, provider_usage=usage),
    )
    aggregate = summary["provider_usage"]
    assert aggregate["status"] == "complete"
    assert aggregate["input_tokens"] == 22
    assert aggregate["cached_input_tokens"] == 6
    assert aggregate["output_tokens"] == 10
    assert aggregate["reasoning_output_tokens"] == 4
    assert aggregate["latency_ms"] == 15.0
    assert aggregate["api_price_proxy_micros"] == 246
    assert aggregate["billing_bases"] == [campaign.OAUTH_BILLING_BASIS]
    assert summary["arms"]["hybrid"]["provider_usage"]["calls_observed"] == 1

    path = tmp_path / "manifest.json"
    path.write_text(canonical_json(manifest))
    public = campaign.public_report(path, summary)
    assert public["metrics"]["provider_usage"] == aggregate
    accounting = public["protocol"]["token_accounting"]
    assert accounting["transport"] == "codex_oauth"
    assert accounting["billing_basis"] == campaign.OAUTH_BILLING_BASIS


def test_usage_summary_marks_missing_failed_call_counters_explicitly():
    summary = campaign._provider_usage_summary([
        {"status": "complete", "provider_usage": [{
            "input_tokens": 1, "output_tokens": 1, "total_tokens": 2,
            "transport_identity": "codex_oauth",
            "billing_basis": campaign.OAUTH_BILLING_BASIS,
        }]},
        {"status": "error", "provider_usage": []},
    ])
    assert summary["status"] == "partial"
    assert summary["rows_without_usage"] == 1
    assert summary["failed_rows_without_usage"] == 1
    assert summary["unmetered_peer_internal_calls"] == "not surfaced by the row contract"


def test_reader_cannot_escape_declared_task_files(tmp_path):
    scenario = SimpleNamespace(task=SimpleNamespace(target_files=("service.py",)))
    response = {"answer": "", "citations": [], "files": {"../oracle.py": "pass"}}
    with pytest.raises(ValueError, match="undeclared"):
        campaign.apply_reader_files(response, scenario, tmp_path)
    assert not (tmp_path.parent / "oracle.py").exists()


def test_approval_is_bound_to_stage_and_location_before_sdk_init(tmp_path):
    path = tmp_path / "approval.json"
    path.write_text(json.dumps({"schema": "engraphis-campaign-stage-approval/v1",
                               "campaign_sha256": "b" * 64, "stage": "development_pilot"}))
    with pytest.raises(ValueError, match="exact campaign"):
        campaign.approved_client(small_manifest(), "development_pilot", path, tmp_path)


def test_peer_internal_calls_have_one_durable_namespace_and_bounded_inputs():
    manifest = small_manifest()
    stage = {**manifest["stages"]["development_pilot"], "max_peer_internal_calls_per_attempt": 1}
    calls = []
    client = SimpleNamespace(complete=lambda **kwargs: calls.append(kwargs))
    proxy = campaign.AttemptBudgetClient(client, "test-attempt", stage)
    proxy.complete(call_id="untrusted-collision", kind="evaluator", input="short", max_output_tokens=50000)
    assert calls[0]["call_id"] == "test-attempt-internal-0"
    assert calls[0]["kind"] == "ingest"
    assert calls[0]["max_output_tokens"] == 4096
    with pytest.raises(ValueError, match="call ceiling"):
        proxy.complete(input="repeat", max_output_tokens=1)


def test_proposal_includes_ingestion_corrections_and_cache_write_ceiling():
    manifest = small_manifest()
    manifest["stages"]["development_pilot"]["arms"] = ["mem0", "graphiti"]
    proposal = campaign.budget_proposal(manifest, "development_pilot")
    assert proposal["approved"] is False
    assert proposal["reader_calls_max"] == 2
    assert proposal["correction_calls_max"] == 2
    assert proposal["ingestion_extraction_calls_max"] == 64
    assert proposal["max_calls"] == 68
    assert proposal["max_cost_micros"] == 68 * 13108


def test_run_attempt_does_not_send_oracle_or_answers_to_reader(tmp_path, monkeypatch):
    source = tmp_path / "source.json"
    source.write_text("{}")
    oracle_path = tmp_path / "oracle.py"
    oracle_path.write_text("# SECRET_ORACLE")
    task = SimpleNamespace(prompt="Fix the public function.", target_files=("service.py",),
                           required_evidence_ids=("gold-secret",), forbidden_evidence_ids=(),
                           untrusted_evidence_ids=(), answer_tokens=("ANSWER_KEY",), answerable=True)
    scenario = SimpleNamespace(id="fixture-a", source_path=source, source_sha256=sha256_file(source),
                               oracle_path=oracle_path, oracle_sha256=sha256_file(oracle_path), task=task,
                               family_id="family-a", category="corrections")
    from contextlib import contextmanager

    @contextmanager
    def workspace(_scenario, target):
        target.mkdir()
        (target / "service.py").write_text("def result(): return 0")
        yield target

    monkeypatch.setattr(campaign, "scenario_workspace", workspace)
    calls = []

    def complete(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text=json.dumps({"answer": "Done", "citations": [], "files": {}}),
                               usage=SimpleNamespace(as_dict=lambda: {"input_tokens": 10, "output_tokens": 5}))

    manifest = {**small_manifest(), "source": {}, "docker_image": "unused"}
    observed = campaign.run_attempt(manifest, "development_pilot", campaign.cells(manifest, "development_pilot")[0],
                                    SimpleNamespace(get=lambda _: scenario), SimpleNamespace(complete=complete),
                                    oracle=lambda *args: {"passed": True, "timed_out": False})
    assert observed["task_success"] is True
    assert observed["evidence_retention"] == 0.0
    assert observed["citation_validity"] is False
    assert observed["answer_completeness"] is None
    assert "ANSWER_KEY" not in str(calls)
    assert "SECRET_ORACLE" not in str(calls)
    assert "gold-secret" not in str(calls)


def test_campaign_execution_lock_blocks_parallel_dispatch(tmp_path):
    (tmp_path / ".campaign-execution.lock").write_text("other runner")
    with pytest.raises(ValueError, match="another campaign"):
        campaign.execute(small_manifest(), "development_pilot", tmp_path, None, None)


def test_last_attempt_critical_violation_blocks_completion(tmp_path):
    manifest = small_manifest()
    summary = campaign.execute(manifest, "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c, critical_violations=["unsupported_assertion"]))
    assert summary["status"] == "BLOCKED"
    assert summary["critical_violations"] == 1
    assert not list((tmp_path / "development_pilot").glob("*.started"))


def test_full_history_excludes_future_poison_and_sibling_sessions():
    from eval.coding_corpus import load_corpus
    corpus = load_corpus()
    for category in ("temporal_history", "poisoning", "scope_boundaries"):
        scenario = next(item for item in corpus.scenarios("development") if item.category == category)
        context, ids = campaign._full_history(scenario, 4096)
        assert all(not identity.startswith(("future:", "untrusted:", "session:", "forbidden:")) for identity in ids)
        assert "Ignore the review boundary" not in context
        assert set(scenario.task.required_evidence_ids) <= set(ids)


def test_validation_selection_requires_actual_checkpoints(tmp_path):
    manifest = small_manifest()
    manifest["stages"]["validation"] = manifest["stages"]["development_pilot"]
    manifest["source"] = {}
    with pytest.raises(ValueError, match="every attempt"):
        campaign.validation_selection(manifest, tmp_path)
    # Injecting a standalone claimed COMPLETE status cannot satisfy this check.
    (tmp_path / "claimed-selection.json").write_text('{"validation_status":"COMPLETE"}')
    with pytest.raises(ValueError, match="every attempt"):
        campaign.validation_selection(manifest, tmp_path)


def test_invalid_metric_is_recorded_as_error(tmp_path):
    summary = campaign.execute(small_manifest(), "development_pilot", tmp_path, None, None,
                               attempt_runner=lambda m, s, c, *args: row(c, context_tokens=513))
    assert summary["statuses"] == {"error": 1}
    assert summary["status"] == "BLOCKED"


def test_manifest_cannot_rehash_a_reduced_stage(monkeypatch, tmp_path):
    from eval.coding_corpus import load_corpus
    corpus = load_corpus()
    monkeypatch.setattr(campaign, "source_snapshot", lambda: {})
    lock = tmp_path / "environment.json"
    lock.write_text("{}")
    manifest, companion = campaign.make_manifest(embed_model="test", embed_revision="a" * 40, dependency_lock=lock)
    manifest["stages"]["held_out"]["scenario_ids"] = [corpus.scenarios("held_out")[0].id]
    manifest["binding_sha256"] = campaign.digest({key: value for key, value in manifest.items() if key != "binding_sha256"})
    with pytest.raises(ValueError, match="frozen split"):
        campaign.validate_manifest(manifest, companion, live=False)


def test_manifest_rejects_secret_oauth_fields_before_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "source_snapshot", lambda: {})
    lock = tmp_path / "environment.json"
    lock.write_text("{}")
    oauth = campaign._codex_oauth_configuration()
    oauth["password"] = "must-not-enter-the-binding"

    with pytest.raises(ValueError, match="unsupported fields"):
        campaign.make_manifest(
            embed_model="test", embed_revision="a" * 40, dependency_lock=lock,
            oauth_configuration=oauth,
        )
