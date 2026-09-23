from scripts import export_offline_evidence


def test_future_offline_export_preserves_packed_quality_and_boundaries(monkeypatch):
    chunking = {
        "reports": {
            "whole": {
                "questions": 2,
                "recall_at_k": 1.0,
                "mean_context_tokens": 40.0,
                "mean_evidence_tokens": 20.0,
                "max_stored_tokens": 50,
                "memories_stored": 2,
            },
            "chunked": {
                "questions": 2,
                "token_counter": "engraphis.chars4.v1",
                "recall_at_k": 1.0,
                "mean_context_tokens": 10.0,
                "mean_evidence_tokens": 6.0,
                "max_stored_tokens": 12,
                "memories_stored": 4,
            },
        },
        "context_reduction_pct": 75.0,
    }
    performance = {
        "quality": {
            "recall_at_k": 1.0,
            "hit_at_k": 1.0,
            "answer_token_recall": 1.0,
        },
        "packed_quality": {
            "recall_at_k": 0.75,
            "hit_at_k": 1.0,
            "answer_token_recall": 0.5,
            "sample_count": 2,
        },
        "quality_scope": {
            "retrieved": "candidate page",
            "packed": "reader context",
        },
        "payload_boundary": {
            "kind": "serialized_json_shape_proxy",
            "transport_measured": False,
            "mcp_envelope_serialized": False,
            "token_counter": "engraphis.regex.v1",
        },
        "context": {
            "mean_tokens": 8.0,
            "max_tokens": 12,
            "token_counter": "engraphis.regex.v1",
            "full_serialized_payload_tokens": 30,
            "compact_serialized_payload_tokens": 15,
            "saved_serialized_payload_tokens": 15,
            "serialized_payload_savings_ratio": 0.5,
        },
        "corpus": {"dataset_cases": 1, "memories": 4, "questions": 2},
        "run": {"timed_recalls": 20},
    }
    grounded = {
        "n_answerable": 1,
        "grounded_hits": 1,
        "n_unanswerable": 1,
        "n_quarantine": 0,
        "abstain_hits": 1,
        "quarantine_hits": 0,
        "accuracy": 1.0,
    }

    monkeypatch.setattr(
        export_offline_evidence,
        "source_manifest",
        lambda: {"fixture.py": "a" * 64},
    )
    monkeypatch.setattr("eval.chunking_eval.load", lambda _path: ["document"])
    monkeypatch.setattr("eval.chunking_eval.compare", lambda *_args, **_kwargs: chunking)
    monkeypatch.setattr("eval.harness.load_dataset", lambda _path: ["question"])
    monkeypatch.setattr("eval.performance.run", lambda *_args, **_kwargs: performance)
    monkeypatch.setattr("eval.grounded.run", lambda: grounded)

    report = export_offline_evidence.generate()
    exported = next(run["result"] for run in report["runs"] if run["id"] == "offline-performance")

    assert {
        key: exported[key]
        for key in ("recall_at_k", "hit_at_k", "answer_token_recall")
    } == performance["quality"]
    assert exported["packed_quality"] == performance["packed_quality"]
    assert exported["quality_scope"] == performance["quality_scope"]
    assert exported["payload_boundary"] == performance["payload_boundary"]
