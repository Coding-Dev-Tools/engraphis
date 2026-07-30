"""Focused contracts for deterministic retrieval-profile routing."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from engraphis.core.engine import MemoryEngine
from engraphis.core.interfaces import MemoryType, Scope
from engraphis.core.retrieval_policy import (
    DeterministicRetrievalPolicy,
    ProfileConfig,
    profile_config,
)
from eval.harness import _seed_case_graph, load_dataset


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("balanced", (True, True, True, False)),
        ("lexical", (False, True, False, False)),
        ("graph", (True, True, True, False)),
        ("code", (True, True, True, True)),
    ],
)
def test_concrete_profiles_have_stable_arm_configurations(
    name: str, expected: tuple[bool, bool, bool, bool]
) -> None:
    config = profile_config(name)

    assert (config.vector, config.lexical, config.graph, config.code) == expected
    with pytest.raises(FrozenInstanceError):
        config.code = False  # type: ignore[misc]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Trace src/api.py -> Handler.handle()", "code"),
        ("Why does checkout depend on auth?", "graph"),
        ('Find the exact "RATE_LIMIT" identifier.', "lexical"),
        ("What did we decide for the launch?", "balanced"),
        ("Why does Handler.handle() call the API_KEY module?", "code"),
    ],
)
def test_auto_routing_is_deterministic_and_uses_specific_signals_first(
    query: str, expected: str
) -> None:
    policy = DeterministicRetrievalPolicy()

    assert policy.profile(query) == expected
    assert policy.resolve("auto", query).name == expected


@pytest.mark.parametrize(
    ("requested", "query", "expected"),
    [
        ("balanced", "src/api.py -> Handler.handle()", "balanced"),
        ("AUTO", "Find exact RATE_LIMIT", "lexical"),
        (" lexical ", "Why does checkout depend on auth?", "lexical"),
        ("graph", "Find exact RATE_LIMIT", "graph"),
        ("code", "What did we decide for the launch?", "code"),
    ],
)
def test_explicit_profile_overrides_auto_routing(
    requested: str, query: str, expected: str
) -> None:
    policy = DeterministicRetrievalPolicy()

    assert policy.resolve(requested, query) == profile_config(expected)


@pytest.mark.parametrize("requested", ["rerank", "semantic", "auto-plus"])
def test_unknown_requested_profile_is_rejected(requested: str) -> None:
    with pytest.raises(ValueError, match="retrieval_profile"):
        DeterministicRetrievalPolicy().resolve(requested, "ordinary query")


def test_empty_requested_profile_defaults_to_balanced() -> None:
    assert DeterministicRetrievalPolicy().resolve("", "src/api.py -> Handler.handle()").name == "balanced"


@pytest.mark.parametrize("name", ["auto", "", "unknown"])
def test_profile_config_requires_a_concrete_profile(name: str) -> None:
    with pytest.raises(ValueError, match="resolve to one of"):
        profile_config(name)


def test_profile_config_returns_an_immutable_value_object() -> None:
    config = profile_config("code")

    assert isinstance(config, ProfileConfig)
    assert config.name == "code"


def test_auto_graph_profile_prioritizes_multi_hop_evidence_without_changing_balanced():
    dataset = (
        Path(__file__).resolve().parents[1] / "eval" / "datasets" / "graph_multihop.jsonl"
    )
    case = load_dataset(str(dataset))[0]
    assert profile_config("balanced").graph_scale == 1.0
    engine = MemoryEngine.create(":memory:")
    workspace_id = engine.store.get_or_create_workspace("eval")
    repo_id = engine.store.get_or_create_repo(workspace_id, case["id"])
    _seed_case_graph(
        engine.store,
        workspace_id=workspace_id,
        repo_id=repo_id,
        case=case,
    )
    by_tag = {}
    for memory in case["memories"]:
        by_tag[memory["tag"]] = engine.remember(
            memory["text"],
            workspace_id=workspace_id,
            repo_id=repo_id,
            mtype=MemoryType.EPISODIC,
            scope=Scope.REPO,
            resolve_conflicts=False,
        )

    result = engine.recall(
        case["questions"][0]["q"],
        workspace_id=workspace_id,
        repo_id=repo_id,
        k=5,
        retrieval_profile="auto",
        diagnostics=True,
    )

    assert result.retrieval_profile == "graph"
    assert by_tag["m_bill"] in {chunk["id"] for chunk in result.chunks}
    graph_details = [
        item for item in result.retrieval_trace or []
        if item["id"] == by_tag["m_bill"]
    ][0]
    assert graph_details["profile_adjusted"]["graph"] == pytest.approx(
        graph_details["normalized"]["graph"] * 3.0 + 1.5
    )
