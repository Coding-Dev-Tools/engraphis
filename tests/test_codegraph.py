"""Tests for the code-symbol graph extraction backends (MASTER_PLAN.md §9).

``RegexSymbolIndexer`` and ``get_code_indexer``/``detect_lang``/``iter_source_files``
are dependency-free and run in the offline numpy-only gate. The tree-sitter-specific
tests skip cleanly when the optional ``tree_sitter_language_pack`` extra isn't
installed, so they never affect that gate.
"""
import pytest

from engraphis.backends.codegraph import (
    RegexSymbolIndexer,
    detect_lang,
    get_code_indexer,
    iter_source_files,
)

PY_SRC = """
def add(a, b):
    return a + b

class Calculator:
    def __init__(self):
        self.total = 0

    def add(self, x):
        self.total = add(self.total, x)
        return self.total

import os
from collections import OrderedDict
"""


def test_detect_lang_by_extension():
    assert detect_lang("foo.py") == "python"
    assert detect_lang("foo.tsx") == "typescript"
    assert detect_lang("foo.jsx") == "javascript"
    assert detect_lang("foo.txt") is None


def test_get_code_indexer_regex_forced():
    idx = get_code_indexer(prefer="regex")
    assert isinstance(idx, RegexSymbolIndexer)


def test_get_code_indexer_auto_never_raises():
    # Whatever is installed, "auto" must hand back something usable, never crash —
    # the whole point of gating a heavy/fragile optional dependency behind a factory.
    idx = get_code_indexer(prefer="auto")
    assert idx.supports("python")


def test_regex_indexer_finds_function_and_class():
    idx = RegexSymbolIndexer()
    fi = idx.index_file("calc.py", PY_SRC, "python")
    names = {s.name for s in fi.symbols}
    assert "add" in names and "Calculator" in names


def test_regex_indexer_unsupported_language_returns_empty():
    idx = RegexSymbolIndexer()
    fi = idx.index_file("calc.rb", "def add(a,b)\n  a+b\nend\n", "ruby")
    assert fi.symbols == [] and fi.edges == []


def test_iter_source_files_skips_excluded_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a(): pass\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "b.py").write_text("def b(): pass\n")
    (tmp_path / "readme.md").write_text("not code")
    found = sorted(iter_source_files(str(tmp_path)))
    assert any(f.endswith("a.py") for f in found)
    assert not any("node_modules" in f for f in found)
    assert not any(f.endswith("readme.md") for f in found)


# ── tree-sitter-specific behavior (skipped if the optional extra isn't installed) ──

tree_sitter_language_pack = pytest.importorskip(
    "tree_sitter_language_pack", reason="optional code-graph extra not installed")


def test_tree_sitter_indexer_extracts_qualified_names_and_edges():
    from engraphis.backends.codegraph import TreeSitterSymbolIndexer
    idx = TreeSitterSymbolIndexer()
    fi = idx.index_file("calc.py", PY_SRC, "python")
    fqnames = {s.fqname for s in fi.symbols}
    assert "add" in fqnames                  # top-level function
    assert "Calculator" in fqnames            # class
    assert "Calculator.add" in fqnames        # method, qualified by its class
    assert any(e.relation == "calls" and e.dst == "add" for e in fi.edges)
    assert any(e.relation == "imports" and e.dst == "os" for e in fi.edges)


def test_tree_sitter_indexer_javascript():
    from engraphis.backends.codegraph import TreeSitterSymbolIndexer
    idx = TreeSitterSymbolIndexer()
    js_src = (
        "function add(a, b) { return a + b; }\n"
        "class Calc {\n"
        "  addOne(x) { return add(x, 1); }\n"
        "}\n"
    )
    fi = idx.index_file("calc.js", js_src, "javascript")
    fqnames = {s.fqname for s in fi.symbols}
    assert "add" in fqnames and "Calc.addOne" in fqnames
