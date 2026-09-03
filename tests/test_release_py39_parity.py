"""Python 3.9 parity: the offline core with optional extras absent.

``pyproject.toml`` declares ``requires-python = ">=3.9"``, so this locks the
floor in executable form:

* core modules parse under the 3.9 grammar (no ``match``/``except*``/``type``
  statements that a 3.9 interpreter could not even import);
* code indexing falls back to the dependency-free regex indexer when the
  tree-sitter extra is unavailable;
* resolve, scoring, recall, and grounded recall all work with optional extras
  reported absent through ``importlib.util.find_spec``;
* ``ENGRAPHIS_INDEX_ROOTS`` defaults to the local-first roots and rejects
  relative operator configuration.
"""
import ast
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Optional third-party extras the offline core must never hard-require.
OPTIONAL_EXTRAS = frozenset({
    "mcp",
    "psycopg",
    "psycopg2",
    "pypdf",
    "sqlcipher3",
    "sqlite_vec",
    "transformers",
    "tree_sitter",
    "tree_sitter_language_pack",
})


@pytest.fixture
def no_extras(monkeypatch):
    """Report every optional extra as absent, then make it actually so."""
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if str(name).split(".")[0] in OPTIONAL_EXTRAS:
            return None
        return real_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    for module in OPTIONAL_EXTRAS:
        monkeypatch.setitem(sys.modules, module, None)
    for module in list(sys.modules):
        if module.split(".")[0] in OPTIONAL_EXTRAS and sys.modules[module] is not None:
            monkeypatch.delitem(sys.modules, module, raising=False)
    return fake_find_spec


def test_core_modules_parse_under_39_grammar():
    failures = []
    for path in sorted((ROOT / "engraphis" / "core").glob("*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, filename=str(path), feature_version=(3, 9))
        except SyntaxError as exc:
            failures.append(f"{path.name}:{exc.lineno}: {exc.msg}")
    assert not failures, "3.10+ grammar in 3.9-floor core:\n" + "\n".join(failures)


def test_regex_indexer_is_the_fallback_without_tree_sitter(no_extras):
    for extra in ("tree_sitter", "tree_sitter_language_pack", "mcp"):
        assert importlib.util.find_spec(extra) is None
    from engraphis.backends.codegraph import (
        RegexSymbolIndexer,
        get_code_indexer,
    )

    indexer = get_code_indexer("auto")
    assert isinstance(indexer, RegexSymbolIndexer)
    assert indexer.supports("python")
    indexed = indexer.index_file(
        "example.py", "def hello(name):\n    return name\n", "python",
    )
    assert [symbol.name for symbol in indexed.symbols] == ["hello"]
    # The regex fallback still recovers file->symbol structure without tree-sitter.
    assert [(e.src, e.dst, e.relation) for e in indexed.edges] == [
        ("example.py", "hello", "defines")]


def test_resolve_and_scoring_have_no_optional_imports(no_extras):
    from engraphis.core.interfaces import MemoryRecord
    from engraphis.core.resolve import ResolutionOp, resolve
    from engraphis.core import scoring

    neighbor = MemoryRecord(id="mem_old", content="The API rate limit is 100 per minute.")
    decision = resolve("The API rate limit is 100 per minute.", [(0.9, neighbor)])
    assert decision.op == ResolutionOp.NOOP

    record = MemoryRecord(id="mem", content="The API rate limit is 100 per minute.")
    weights = scoring.weights_for(record.mtype)
    assert scoring.score_memory(record, now=1_700_000_000.0, weights=weights) >= 0.0
    assert scoring.score_proactive(record, now=1_700_000_000.0) >= 0.0


def test_recall_and_grounded_work_with_extras_absent(no_extras):
    from engraphis.core.engine import MemoryEngine
    from engraphis.core.interfaces import SearchFilter

    eng = MemoryEngine.create(":memory:")
    wid = eng.store.get_or_create_workspace("w")
    rid = eng.store.get_or_create_repo(wid, "r")
    eng.remember(
        "We standardised on PASETO tokens for auth, replacing JWT.",
        workspace_id=wid, repo_id=rid, title="auth",
    )

    result = eng.recall_engine.recall(
        "which auth scheme did we standardise on?",
        SearchFilter(workspace_id=wid, repo_id=rid),
        k=2,
    )
    assert result.count >= 1

    answer = eng.grounded_recall(
        "which auth scheme did we standardise on?", workspace_id=wid, repo_id=rid,
    )
    assert answer.grounded and not answer.abstained
    assert "paseto" in answer.answer.lower()

    abstained = eng.grounded_recall(
        "how do I bake sourdough bread?", workspace_id=wid, repo_id=rid,
    )
    assert abstained.abstained and not abstained.grounded


def test_index_roots_default_to_local_first(monkeypatch):
    from engraphis.core.engine import _approved_local_index_roots

    monkeypatch.delenv("ENGRAPHIS_INDEX_ROOTS", raising=False)
    monkeypatch.delenv("ENGRAPHIS_HTTP_INDEX_ROOT", raising=False)

    expected = tuple(
        os.path.normcase(os.path.realpath(path))
        for path in (os.getcwd(), os.path.expanduser("~"), tempfile.gettempdir())
    )
    assert _approved_local_index_roots() == expected


def test_index_roots_reject_relative_operator_paths(monkeypatch):
    from engraphis.core.engine import _approved_local_index_roots

    monkeypatch.setenv("ENGRAPHIS_INDEX_ROOTS", os.path.join("relative", "path"))
    with pytest.raises(ValueError, match="absolute"):
        _approved_local_index_roots()
