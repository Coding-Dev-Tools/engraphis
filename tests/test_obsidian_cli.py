"""Console, compatibility, and packaging contracts for the v2 Obsidian importer."""
from __future__ import annotations

import json
import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from engraphis.service import MemoryService
from scripts import entry, importer, seed_from_obsidian, smoke_entry_points


ROOT = Path(__file__).resolve().parents[1]


def _vault(tmp_path: Path, count: int = 2) -> Path:
    vault = tmp_path / "Vault"
    vault.mkdir()
    for index in range(count):
        (vault / f"Note-{index}.md").write_text(
            f"# Note {index}\nLocal content.\n", encoding="utf-8",
        )
    return vault


def test_dry_run_rejects_invalid_scope_without_creating_database(tmp_path, capsys):
    vault = _vault(tmp_path)
    database = tmp_path / "missing.db"

    result = importer.main([
        "obsidian", str(vault), "--db", str(database), "--workspace", "acme",
        "--scope", "session", "--dry-run",
    ])

    assert result == 2
    assert "session scope requires --session" in capsys.readouterr().err
    assert not database.exists()


def test_dry_run_new_workspace_cannot_reuse_another_workspaces_vault(
    tmp_path, capsys,
):
    vault = _vault(tmp_path, count=1)
    database = tmp_path / "memory.db"
    service = MemoryService.create(
        str(database), embed_dim=64, extractor="none", graph_extractor="none",
        retention_supervisor="none",
    )
    try:
        imported = service.import_obsidian_vault(
            str(vault), workspace="alpha", vault_label=vault.name,
            confirmed=True,
        )
        foreign_vault_id = imported["vault_id"]
    finally:
        service.close()

    result = importer.main([
        "obsidian", str(vault), "--db", str(database), "--workspace", "beta",
        "--dry-run", "--json",
    ])

    assert result == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["vault_id"] is None
    assert preview["source_id"] is None
    assert preview["counts"]["imported"] == 1
    assert preview["counts"].get("skipped", 0) == 0
    assert foreign_vault_id not in str(preview)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM workspaces WHERE name='beta'"
        ).fetchone()[0] == 0


def test_limit_uses_service_cancellation_boundary_and_reports_partial(monkeypatch, tmp_path, capsys):
    vault = _vault(tmp_path, count=3)
    scan = SimpleNamespace(notes=[object(), object(), object()])
    calls = {}

    class Service:
        def import_obsidian_vault(self, _path, **kwargs):
            calls.update(kwargs)
            assert kwargs["cancel_check"]() is False
            kwargs["progress"]({"status": "imported", "relative_path": "Note-0.md"})
            assert kwargs["cancel_check"]() is True
            return {"state": "cancelled", "counts": {"imported": 1}}

        def close(self):
            calls["closed"] = True

    monkeypatch.setattr(importer, "scan_obsidian_vault", lambda _path: scan)
    monkeypatch.setattr(importer, "_preview", lambda *_args: {
        "state": "preview", "counts": {"markdown": 3}, "summary": {}, "files": [],
    })
    monkeypatch.setattr(importer, "_local_service", lambda _path: Service())

    result = importer.main([
        "obsidian", str(vault), "--workspace", "acme", "--yes", "--json",
        "--limit", "1",
    ])

    assert result == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["limit"] == {"processed": 1, "reached": True, "requested": 1}
    assert calls["confirmed"] is True
    assert calls["closed"] is True


def test_confirmed_cli_import_writes_through_v2_service(monkeypatch, tmp_path, capsys):
    vault = _vault(tmp_path, count=1)
    database = tmp_path / "memory.db"
    monkeypatch.setattr(
        importer,
        "_local_service",
        lambda path: MemoryService.create(
            path, embed_dim=64, extractor="none", graph_extractor="none",
            retention_supervisor="none",
        ),
    )

    result = importer.main([
        "obsidian", str(vault), "--db", str(database), "--workspace", "acme",
        "--yes", "--json",
    ])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["report"]["state"] == "completed"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1


def test_confirmed_cli_import_uses_the_exact_previewed_scan(monkeypatch, tmp_path, capsys):
    vault = _vault(tmp_path, count=1)
    note = vault / "Note-0.md"
    note.write_text("# Approved\nPREVIEWED_BYTES\n", encoding="utf-8")
    database = tmp_path / "memory.db"
    monkeypatch.setattr(
        importer,
        "_local_service",
        lambda path: MemoryService.create(
            path, embed_dim=64, extractor="none", graph_extractor="none",
            retention_supervisor="none",
        ),
    )

    def mutate_after_preview(_args):
        note.write_text("# Changed\nUNPREVIEWED_BYTES\n", encoding="utf-8")
        return True

    monkeypatch.setattr(importer, "_confirm", mutate_after_preview)
    result = importer.main([
        "obsidian", str(vault), "--db", str(database), "--workspace", "acme",
        "--yes", "--json",
    ])

    assert result == 0
    assert json.loads(capsys.readouterr().out)["report"]["state"] == "completed"
    with sqlite3.connect(database) as connection:
        content = connection.execute("SELECT content FROM memories").fetchone()[0]
    assert "PREVIEWED_BYTES" in content
    assert "UNPREVIEWED_BYTES" not in content


def test_legacy_wrapper_maps_namespace_limit_and_confirmation(monkeypatch, capsys):
    forwarded = []
    monkeypatch.setattr(importer, "main", lambda argv: forwarded.extend(argv) or 3)

    result = seed_from_obsidian.main([
        "C:/vault", "--namespace", "legacy", "--limit", "4", "--json",
    ])

    assert result == 3
    assert forwarded[:4] == ["obsidian", "C:/vault", "--workspace", "legacy"]
    assert ["--limit", "4"] == forwarded[forwarded.index("--limit"):][:2]
    assert "--yes" in forwarded
    assert "deprecated" in capsys.readouterr().err.casefold()


def test_front_door_dispatches_import_without_a_second_implementation(monkeypatch):
    captured = []
    module = SimpleNamespace(main=lambda: captured.append(list(entry.sys.argv)) or 0)
    monkeypatch.setattr(entry, "import_module", lambda name: module)

    assert entry.main(["import", "obsidian", "C:/vault", "--dry-run"]) == 0
    assert captured == [["engraphis import", "obsidian", "C:/vault", "--dry-run"]]


def test_import_console_alias_matches_distribution_and_artifact_manifest():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'engraphis-import = "scripts.importer:main"' in pyproject
    assert smoke_entry_points.EXPECTED_ENTRY_POINTS["engraphis-import"] == (
        "scripts.importer:main"
    )


def test_reports_survive_windows_charmap_and_escape_terminal_controls(monkeypatch):
    sink = io.BytesIO()
    stream = io.TextIOWrapper(sink, encoding="cp1252")
    monkeypatch.setattr(importer.sys, "stdout", stream)

    importer._json({"path": "Notes/進捗.md"})
    importer._console("Notes/line\nbreak-進捗.md")
    stream.flush()

    output = sink.getvalue()
    assert b"Notes/\\u9032\\u6357.md" in output
    assert b"Notes/line\\x0abreak-\\u9032\\u6357.md" in output


@pytest.mark.parametrize("value", ["-1", "not-an-integer"])
def test_limit_rejects_invalid_values_without_scanning(monkeypatch, value):
    monkeypatch.setattr(
        importer, "scan_obsidian_vault",
        lambda _path: pytest.fail("invalid input must fail before scanning"),
    )
    with pytest.raises(SystemExit) as exc:
        importer.main(["obsidian", "C:/vault", "--limit", value])
    assert exc.value.code == 2
