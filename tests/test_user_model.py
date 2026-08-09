import math

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



def test_user_model_rejects_nonfinite_persisted_and_runtime_scores():
    model = UserModel.from_dict({
        "topics": {"auth": float("nan")},
        "mtypes": {"semantic": float("inf")},
        "sources": {"manual": float("-inf")},
        "detail_level": float("nan"),
        "interactions": float("inf"),
    })
    model.update_from_interaction(
        "auth",
        [{"content": "Auth fact.", "mtype": "semantic"}],
        Feedback(rating=float("nan")),
    )
    ranked = model.bias_recall(
        "auth",
        [{"id": "bad", "content": "Auth fact.", "score": float("inf")}],
        strength=float("nan"),
    )

    assert all(math.isfinite(value) for value in model.topics.values())
    assert all(math.isfinite(value) for value in model.mtypes.values())
    assert all(math.isfinite(value) for value in model.sources.values())
    assert math.isfinite(model.detail_level)
    assert model.interactions == 1
    assert math.isfinite(ranked[0]["base_score"])
    assert math.isfinite(ranked[0]["score"])


def test_unrelated_learned_topic_cannot_overwhelm_current_query_relevance():
    model = UserModel()
    for _ in range(30):
        model.update_from_interaction(
            "authentication tokens",
            [{"content": "Authentication token rotation.", "mtype": "semantic",
              "provenance": {"source": "manual"}}],
            Feedback(rating=1.0),
        )

    ranked = model.bias_recall(
        "database migration",
        [
            {"id": "database", "content": "Database migration checklist.",
             "score": 0.51, "mtype": "semantic",
             "provenance": {"source": "manual"}},
            {"id": "favorite", "content": "Authentication token rotation.",
             "score": 0.50, "mtype": "semantic",
             "provenance": {"source": "manual"}},
        ],
        strength=1.0,
    )

    assert ranked[0]["id"] == "database"
    assert ranked[1]["personalization"]["query_topic_hits"] == []