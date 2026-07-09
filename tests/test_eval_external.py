import json

from eval.external import load_locomo, load_longmemeval
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
    assert len(case["questions"]) == 1                 # adversarial (no evidence) skipped
    assert case["questions"][0]["supporting"] == ["D1:1"]


def test_load_longmemeval_sessions_and_abstention(tmp_path):
    cases = load_longmemeval(_longmemeval_fixture(tmp_path))
    assert len(cases) == 1                             # _abs instance skipped
    case = cases[0]
    assert {m["tag"] for m in case["memories"]} == {"s1", "s2"}
    assert "pnpm" in case["memories"][0]["text"]
    assert case["questions"][0]["supporting"] == ["s1"]


def test_external_cases_run_through_the_real_harness(tmp_path):
    cases = load_locomo(_locomo_fixture(tmp_path))
    report = run(cases, k=3)                           # offline deterministic embedder
    assert report["questions"] == 1
    assert report["recall_at_k"] == 1.0                # evidence found in a 3-memory haystack
