"""The demo seed must show a live supersession, a cited recall, and safe paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from engraphis.service import MemoryService
from scripts.demo import DEMO_FACTS, create_demo_database, main, seed_demo


def test_seed_demo_supersedes_the_old_auth_fact_and_recalls_the_new_one():
    service = MemoryService.create(
        ":memory:",
        embed_model="",
        extractor="none",
        graph_extractor="regex",
        retention_supervisor="none",
    )
    try:
        results = seed_demo(service)
        assert len(results) == len(DEMO_FACTS)

        old = next(
            result
            for result, fact in zip(results, DEMO_FACTS)
            if fact["content"] == "Checkout API uses JWT access tokens."
        )
        new = next(
            result
            for result, fact in zip(results, DEMO_FACTS)
            if fact["content"] == "Checkout API uses PASETO access tokens."
        )
        assert old["id"] in new["superseded"]

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


def test_create_demo_database_seeds_a_file_backed_store(tmp_path):
    db_path = tmp_path / "demo.db"
    results, sample = create_demo_database(db_path)

    assert db_path.is_file()
    assert len(results) == len(DEMO_FACTS)
    assert sum(len(result.get("superseded") or []) for result in results) == 1
    assert "PASETO" in sample["context"]


def test_create_demo_database_refuses_an_existing_database(tmp_path):
    db_path = tmp_path / "demo.db"
    db_path.write_bytes(b"existing database")

    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        create_demo_database(db_path)


def test_create_demo_database_refuses_a_wal_companion(tmp_path):
    db_path = tmp_path / "demo.db"
    Path(str(db_path) + "-wal").write_bytes(b"wal")

    with pytest.raises(SystemExit, match="companion"):
        create_demo_database(db_path)


def test_create_demo_database_restores_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_EMBED_MODEL", "keep-me")

    create_demo_database(tmp_path / "demo.db")

    assert os.environ["ENGRAPHIS_EMBED_MODEL"] == "keep-me"


def test_main_help_exits_zero_without_creating_a_database(monkeypatch):
    for name in ("ENGRAPHIS_EMBED_MODEL", "ENGRAPHIS_DB_PATH"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "ENGRAPHIS_DB_PATH" not in os.environ


def test_demo_facts_stay_in_the_two_demo_workspaces():
    workspaces = {fact["workspace"] for fact in DEMO_FACTS}
    repos = {fact["repo"] for fact in DEMO_FACTS}
    assert workspaces == {"acme-api", "acme-web"}
    assert repos == {"checkout", "billing", "dashboard"}
    assert len({fact["subject_key"] for fact in DEMO_FACTS}) == len(DEMO_FACTS) - 1
