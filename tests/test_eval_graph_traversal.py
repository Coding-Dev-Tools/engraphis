from eval.graph_traversal import run


def test_intent_layered_graph_eval_recovers_each_relation_target():
    report = run()
    assert report["tasks"] == 3
    assert report["uniform_recall_at_1"] == 0.0
    assert report["intent_layered_recall_at_1"] == 1.0
    assert {row["preferred_layer"] for row in report["rows"]} == {
        "causal", "temporal", "entity",
    }
