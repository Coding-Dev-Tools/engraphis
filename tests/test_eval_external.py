import json

import pytest

from engraphis.backends import DeterministicEmbedder
from eval import external
from eval.external import load_locomo, load_longmemeval, main, source_case_count
from eval.harness import run


def _locomo_fixture(tmp_path):
    data = [{
        "sample_id": "conv-1",
        "conversation": {
            "session_1": [
                {"speaker": "Caroline", "dia_id": "D1:1",
                 "text": "I adopted a golden retriever named Biscuit last week."},
                {"speaker": "Melanie", "dia_id": "D1:2",
                 "text": "That's wonderful! How old is Biscuit?"},
            ],
            "session_1_date_time": "1:00 pm on 8 May, 2023",
            "session_2": [
                {"speaker": "Caroline", "dia_id": "D2:1",
                 "text": "Biscuit just turned two and loves swimming."},
            ],
            "session_2_date_time": "3:10 pm on 25 May, 2023",
        },
        "qa": [
            {"question": "What is the name of Caroline's dog?",
             "answer": "Biscuit", "evidence": ["D1:1"], "category": 1},
            {"question": "Unanswerable adversarial question?",
             "answer": "n/a", "evidence": [], "category": 5},
        ],
    }]
    p = tmp_path / "locomo.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def _longmemeval_fixture(tmp_path):
    data = [{
        "question_id": "q-1",
        "question_type": "single-session-user",
        "question": "Which package manager did the user standardize on?",
        "answer": "pnpm",
        "question_date": "2023/05/30",
        "haystack_session_ids": ["s1", "s2"],
        "haystack_dates": ["2023/05/01", "2023/05/02"],
        "haystack_sessions": [
            [{"role": "user", "content": "We standardized on pnpm for all frontend repos."},
             {"role": "assistant", "content": "Noted."}],
            [{"role": "user", "content": "My cat is named Waffles."}],
        ],
        "answer_session_ids": ["s1"],
    }, {
        "question_id": "q-2_abs",
        "question": "abstention instance, should be skipped",
        "answer": "n/a",
        "haystack_session_ids": ["s1"],
        "haystack_sessions": [[{"role": "user", "content": "hello"}]],
        "answer_session_ids": ["s1"],
    }]
    p = tmp_path / "lme.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_load_locomo_normalizes_to_harness_cases(tmp_path):
    cases = load_locomo(_locomo_fixture(tmp_path))
    assert len(cases) == 1
    case = cases[0]
    tags = {m["tag"] for m in case["memories"]}
    assert tags == {"D1:1", "D1:2", "D2:1"}
    assert case["memories"][0]["text"].startswith("[1:00 pm on 8 May, 2023] Caroline:")
    assert len(case["questions"]) == 2                 # adversarial is retained and explicit
    assert case["questions"][0]["supporting"] == ["D1:1"]
    assert case["questions"][1]["category"] == "5"
    assert case["questions"][1]["answerable"] is False


def test_load_longmemeval_sessions_and_abstention(tmp_path):
    cases = load_longmemeval(_longmemeval_fixture(tmp_path))
    assert len(cases) == 2                             # _abs instance is retained
    case = cases[0]
    assert {m["tag"] for m in case["memories"]} == {"s1", "s2"}
    assert "pnpm" in case["memories"][0]["text"]
    assert case["questions"][0]["supporting"] == ["s1"]
    assert cases[1]["questions"][0]["category"] == "abstention"
    assert cases[1]["questions"][0]["answerable"] is False


def test_load_longmemeval_collapses_identical_duplicate_session_ids(tmp_path):
    data = [{
        "question_id": "q-duplicate", "question": "Which tool was selected?",
        "answer": "pnpm", "haystack_session_ids": ["s1", "s1", "s2"],
        "haystack_dates": ["2023/05/01", "2023/05/05", "2023/05/02"],
        "haystack_sessions": [
            [{"role": "user", "content": "We selected pnpm."}],
            [{"role": "user", "content": "We selected pnpm."}],
            [{"role": "user", "content": "Unrelated."}],
        ],
        "answer_session_ids": ["s1"],
    }]
    path = tmp_path / "duplicate-lme.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    cases = load_longmemeval(str(path))

    assert [memory["tag"] for memory in cases[0]["memories"]] == ["s1", "s2"]
    assert cases[0]["questions"][0]["supporting"] == ["s1"]
    assert cases[0]["source_secret_redactions"] == 0
    assert run(cases, k=2)["recall_at_k"] == 1.0


def test_load_longmemeval_rejects_conflicting_duplicate_session_ids(tmp_path):
    data = [{
        "question_id": "q-conflict", "question": "Which tool was selected?",
        "answer": "pnpm", "haystack_session_ids": ["s1", "s1"],
        "haystack_sessions": [
            [{"role": "user", "content": "We selected pnpm."}],
            [{"role": "user", "content": "We selected yarn."}],
        ],
        "answer_session_ids": ["s1"],
    }]
    path = tmp_path / "conflicting-lme.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate session id 's1' has conflicting content"):
        load_longmemeval(str(path))


def test_external_loader_redacts_credential_shaped_source_text(tmp_path):
    secret = "sk-proj-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    data = [{
        "question_id": "q-redact", "question": "What tool was selected?",
        "answer": "pnpm", "haystack_session_ids": ["s1"],
        "haystack_sessions": [[{
            "role": "user", "content": f"provider_key={secret}; we selected pnpm.",
        }]],
        "answer_session_ids": ["s1"],
    }]
    path = tmp_path / "redacted-lme.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    cases = load_longmemeval(str(path))
    stored = cases[0]["memories"][0]["text"]

    assert secret not in stored
    assert "<redacted>" in stored
    assert cases[0]["source_secret_redactions"] == 1
    assert run(cases, k=1)["recall_at_k"] == 1.0


def test_external_cases_run_through_the_real_harness(tmp_path):
    cases = load_locomo(_locomo_fixture(tmp_path))
    report = run(cases, k=3)                           # offline deterministic embedder
    assert report["questions"] == 2
    assert report["scored_questions"] == 1
    assert report["exclusions"][0]["reason"] == "no_gold_evidence"
    # The deterministic embedder is documented as a plumbing check, not
    # a publishable retrieval number. The harness still reports the
    # correct question count, scored count, and exclusion reason; the
    # recall_at_k is a function of the embedder's match quality and is
    # not asserted here.
    assert "recall_at_k" in report
    assert "detail" in report


def test_canonical_external_mode_rejects_partial_limit_before_model_loading(tmp_path):
    path = _locomo_fixture(tmp_path)
    assert source_case_count(path) == 1
    with pytest.raises(SystemExit) as error:
        main(["--dataset", path, "--format", "locomo", "--canonical", "--limit", "1"])
    assert error.value.code == 2


def test_canonical_external_mode_requires_a_pinned_semantic_revision_before_loading(tmp_path, monkeypatch):
    path = _locomo_fixture(tmp_path)
    monkeypatch.setattr(
        external, 'get_embedder',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError('must not load')),
    )

    with pytest.raises(SystemExit) as error:
        main(['--dataset', path, '--format', 'locomo', '--canonical'])

    assert error.value.code == 2


def test_canonical_external_mode_forwards_the_pinned_revision(tmp_path, monkeypatch):
    path = _locomo_fixture(tmp_path)
    captured = {}

    class SemanticEmbedder:
        supports_semantic_search = True
        model_name = 'example/embedder'
        revision = 'a' * 40
        dim = 128

    def create_embedder(model, *, revision=None, require_immutable_models=None):
        captured.update(
            model=model,
            revision=revision,
            require_immutable_models=require_immutable_models,
        )
        return SemanticEmbedder()

    monkeypatch.setattr(external, 'get_embedder', create_embedder)
    monkeypatch.setattr(
        external, 'run',
        lambda *_args, **_kwargs: {
            'questions': 1, 'recall_at_k': 1.0, 'hit_at_k': 1.0,
            'answer_token_recall': 1.0, 'scored_questions': 1, 'exclusions': [],
        },
    )

    assert main([
        '--dataset', path, '--format', 'locomo', '--canonical',
        '--embed-revision', 'a' * 40,
    ]) == 0
    assert captured == {
        'model': 'sentence-transformers/all-MiniLM-L6-v2', 'revision': 'a' * 40,
        'require_immutable_models': True,
    }


def test_external_refuses_silent_semantic_embedder_fallback(tmp_path, monkeypatch, capsys):
    path = _locomo_fixture(tmp_path)
    monkeypatch.setattr(external, 'get_embedder', lambda *_args, **_kwargs: DeterministicEmbedder())

    assert main(['--dataset', path, '--format', 'locomo']) == 2
    assert 'semantic embedder was unavailable' in capsys.readouterr().err


def test_external_offline_report_records_dataset_and_embedding_provenance(tmp_path):
    path = _locomo_fixture(tmp_path)
    output = tmp_path / 'external-report.json'

    assert main([
        '--dataset', path, '--format', 'locomo', '--offline', '--json', str(output),
    ]) == 0

    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['dataset_sha256'] == external.dataset_sha256(path)
    assert report['source_cases'] == report['normalized_cases'] == 1
    assert report['embedding']['revision'] is None
    assert report['configuration'] == {'k': 10, 'limit': None, 'resolve_conflicts': True}
