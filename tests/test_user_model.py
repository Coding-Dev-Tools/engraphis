from engraphis.core.user_model import Feedback, UserModel


def test_user_model_biases_recall_toward_learned_topics_and_sources():
    model = UserModel()
    model.update_from_interaction(
        "auth token migration",
        [{"id": "m1", "title": "Auth", "content": "Use PASETO tokens for API auth.",
          "mtype": "semantic", "provenance": {"source": "manual"}}],
        Feedback(rating=1.0),
    )

    ranked = model.bias_recall("auth work", [
        {"id": "unrelated", "content": "Billing invoices are monthly.", "score": 0.60,
         "mtype": "semantic", "provenance": {"source": "import"}},
        {"id": "preferred", "content": "PASETO tokens secure the API auth flow.",
         "score": 0.55, "mtype": "semantic", "provenance": {"source": "manual"}},
    ], strength=0.8)

    assert ranked[0]["id"] == "preferred"
    assert ranked[0]["base_score"] == 0.55
    assert ranked[0]["personalization"]["topic_hits"]
    assert ranked[0]["score"] > 0.55


def test_negative_feedback_downranks_matching_topic():
    model = UserModel()
    model.update_from_interaction(
        "frontend styling",
        [{"id": "m1", "content": "Tailwind styling conventions.", "mtype": "semantic"}],
        Feedback(rating=-1.0),
    )
    ranked = model.bias_recall("styling", [
        {"id": "styling", "content": "Tailwind styling conventions.", "score": 0.50},
        {"id": "other", "content": "Database migration notes.", "score": 0.49},
    ], strength=1.0)
    assert ranked[0]["id"] == "other"
    assert ranked[1]["personalization"]["preference_score"] < 0


def test_detail_feedback_prefers_concise_or_detailed_results():
    concise = UserModel().update_from_interaction(
        "explain", [{"content": "Short answer."}], Feedback(rating=1.0, detail="concise"))
    detailed = UserModel().update_from_interaction(
        "explain", [{"content": "Long details. " * 80}],
        Feedback(rating=1.0, detail="detailed"))

    results = [
        {"id": "long", "content": "Long details. " * 80, "score": 0.5},
        {"id": "short", "content": "Short answer.", "score": 0.5},
    ]
    assert concise.bias_recall("explain", results, strength=0.5)[0]["id"] == "short"
    assert detailed.bias_recall("explain", results, strength=0.5)[0]["id"] == "long"


def test_user_model_round_trips_to_dict():
    model = UserModel()
    model.update_from_interaction(
        "sqlite persistence",
        [{"content": "SQLite stores local memories.", "mtype": "semantic",
          "provenance": {"source": "manual"}}],
    )
    restored = UserModel.from_dict(model.to_dict())
    assert restored.interactions == model.interactions
    assert restored.topics == model.topics
    assert restored.mtypes == model.mtypes
    assert restored.sources == model.sources
    assert 0.0 <= restored.detail_level <= 1.0
