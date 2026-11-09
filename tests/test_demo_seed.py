"""The demo seed must show a live supersession and a cited recall."""

from __future__ import annotations

from engraphis.service import MemoryService
from scripts.demo import DEMO_FACTS, seed_demo


def test_seed_demo_supersedes_the_old_auth_fact_and_recalls_the_new_one():
    service = MemoryService.create(
        ":memory:",
        extractor="none",
        graph_extractor="regex",
        retention_supervisor="none",
    )
    try:
        results = seed_demo(service)
        assert len(results) == len(DEMO_FACTS)

        invalidations = [result for result in results if result.get("op") == "invalidate"]
        assert len(invalidations) == 1
        assert results[0]["id"] in invalidations[0]["superseded"]

        recall = service.recall(
            "What token format does the checkout API use?",
            workspace="acme-api",
            repo="checkout",
            k=5,
        )
        context = recall["context"]
        assert "PASETO" in context
        assert "Checkout API uses JWT access tokens." not in context

        package_manager = service.recall(
            "Which package manager should the checkout frontend use?",
            workspace="acme-api",
            repo="checkout",
            k=5,
        )
        assert "pnpm" in package_manager["context"]
    finally:
        service.close()


def test_demo_facts_stay_in_the_two_demo_workspaces():
    workspaces = {fact["workspace"] for fact in DEMO_FACTS}
    repos = {fact["repo"] for fact in DEMO_FACTS}
    assert workspaces == {"acme-api", "acme-web"}
    assert repos == {"checkout", "billing", "dashboard"}
    assert len({fact["subject_key"] for fact in DEMO_FACTS}) == len(DEMO_FACTS) - 1
