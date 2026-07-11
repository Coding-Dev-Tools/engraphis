"""Tests for the code-symbol graph extraction backends.

``RegexSymbolIndexer`` and ``get_code_indexer``/``detect_lang``/``iter_source_files``
are dependency-free and run in the offline numpy-only gate. The tree-sitter-specific
tests skip cleanly when the optional ``tree_sitter_language_pack`` extra isn't
installed, so they never affect that gate.
"""
import os

import pytest

from engraphis.backends.codegraph import (
    CompositeSymbolIndexer,
    FileIndex,
    RegexSymbolIndexer,
    Symbol,
    detect_lang,
    get_code_indexer,
    iter_source_files,
    normalize_language,
    supported_languages,
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


# ── C# / C / C++ support (regex path — the one the offline gate exercises) ──────

CSHARP_SRC = """
namespace App {
    public class Service {
        private readonly int _n;
        public Service(int n) { _n = n; }
        public int Compute(int x) {
            if (x > 0) return x;
            return 0;
        }
        internal static string Name() => "svc";
    }
    public interface IThing { }
}
"""

CPP_SRC = """
class Widget {
public:
    int area() {
        return w * h;
    }
};

int add(int a, int b) {
    return a + b;
}

void loop() {
    for (int i = 0; i < 10; i++) {
        add(i, i);
    }
}
"""


def test_detect_lang_covers_csharp_c_cpp():
    assert detect_lang("Foo.cs") == "csharp"
    assert detect_lang("foo.cpp") == "cpp"
    assert detect_lang("foo.hpp") == "cpp"
    assert detect_lang("foo.c") == "c"


def test_regex_indexer_csharp_types_and_methods():
    idx = RegexSymbolIndexer()
    fi = idx.index_file("Service.cs", CSHARP_SRC, "csharp")
    names = {s.name for s in fi.symbols}
    assert {"Service", "IThing", "Compute", "Name"} <= names
    # control-flow must not be mistaken for a definition
    assert "if" not in names


def test_regex_indexer_cpp_classes_and_functions_no_false_positives():
    idx = RegexSymbolIndexer()
    fi = idx.index_file("widget.cpp", CPP_SRC, "cpp")
    names = {s.name for s in fi.symbols}
    assert {"Widget", "area", "add", "loop"} <= names
    # loops/calls are not definitions
    assert "for" not in names and "if" not in names


def test_normalize_language_aliases():
    assert normalize_language("C#") == "csharp"
    assert normalize_language("c++") == "cpp"
    assert normalize_language("Python") == "python"
    assert normalize_language("rust") == "rust"  # unknown passes through, then validated


def test_supported_languages_set():
    langs = supported_languages()
    assert {"python", "javascript", "typescript", "csharp", "c", "cpp"} <= langs
    assert "rust" not in langs


# ── the hang fix: build/generated trees are pruned during the walk ──────────────

def test_iter_source_files_skips_build_output_dirs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cs").write_text("class A {}\n")
    for d in ("bin", "obj", "target"):
        (tmp_path / d).mkdir()
        (tmp_path / d / "g.cs").write_text("class G {}\n")
    found = [f.replace(os.sep, "/") for f in iter_source_files(str(tmp_path))]
    assert any(f.endswith("src/a.cs") for f in found)
    assert not any("/bin/" in f or "/obj/" in f or "/target/" in f for f in found)


# ── .engraphisignore: names, globs, and negation of a default ───────────────────

def test_engraphisignore_names_and_globs(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("def keep(): pass\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "gen.py").write_text("def gen(): pass\n")
    (tmp_path / "a.gen.py").write_text("def a(): pass\n")
    (tmp_path / ".engraphisignore").write_text(
        "# project ignores\n"
        "generated\n"      # bare name → skip this dir/file anywhere
        "*.gen.py\n"       # glob → skip generated sources
    )
    found = [f.replace(os.sep, "/") for f in iter_source_files(str(tmp_path))]
    assert any(f.endswith("src/keep.py") for f in found)
    assert not any("/generated/" in f for f in found)          # name ignore worked
    assert not any(f.endswith("a.gen.py") for f in found)      # glob ignore worked


def test_engraphisignore_negation_cancels_own_pattern(tmp_path):
    # `!name` re-includes a name the ignore file itself excluded (gitignore-style).
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "keep.py").write_text("def k(): pass\n")
    (tmp_path / ".engraphisignore").write_text("logs\n!logs\n")
    found = [f.replace(os.sep, "/") for f in iter_source_files(str(tmp_path))]
    assert any(f.endswith("logs/keep.py") for f in found)


def test_engraphisignore_cannot_re_expose_hardcoded_default(tmp_path):
    # SECURITY: an untrusted repo's ignore file must NOT be able to un-ignore a default
    # excluded dir (that would reintroduce the large-tree hang and pull in vendored code).
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "evil.py").write_text("def evil(): pass\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.py").write_text("def out(): pass\n")
    (tmp_path / ".engraphisignore").write_text("!node_modules\n!build\n")
    found = [f.replace(os.sep, "/") for f in iter_source_files(str(tmp_path))]
    assert not any("/node_modules/" in f for f in found)
    assert not any("/build/" in f for f in found)


def test_symlinked_file_is_not_followed_out_of_root(tmp_path):
    # SECURITY: a source-extension symlink pointing outside the repo must not be read.
    outside = tmp_path.parent / "secret.py"
    outside.write_text("SECRET = 1\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "real.py").write_text("def real(): pass\n")
    try:
        os.symlink(outside, repo / "leak.py")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported on this platform")
    found = [f.replace(os.sep, "/") for f in iter_source_files(str(repo))]
    assert any(f.endswith("real.py") for f in found)
    assert not any(f.endswith("leak.py") for f in found)


def test_engraphisignore_can_be_disabled(tmp_path):
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "gen.py").write_text("def gen(): pass\n")
    (tmp_path / ".engraphisignore").write_text("generated\n")
    found = list(iter_source_files(str(tmp_path), respect_ignore_file=False))
    assert any(f.replace(os.sep, "/").endswith("generated/gen.py") for f in found)


def test_oversized_ignore_file_is_ignored_safely(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def a(): pass\n")
    # a pathological ignore file must not be honoured (and must not hang)
    (tmp_path / ".engraphisignore").write_text("src\n" * 200_000)
    found = list(iter_source_files(str(tmp_path)))
    assert any(f.endswith("a.py") for f in found)


# ── composite indexer: AST where supported, regex fallback per-language ──────────

class _PythonOnlyPrimary:
    def supports(self, lang):
        return lang == "python"

    def index_file(self, file_path, content, lang):
        return FileIndex(symbols=[Symbol(kind="function", name="PRIMARY", fqname="PRIMARY",
                                          file=file_path, span="1-1", lang=lang)])


def test_composite_routes_by_language():
    comp = CompositeSymbolIndexer(_PythonOnlyPrimary(), RegexSymbolIndexer())
    assert comp.supports("python") and comp.supports("csharp")
    # python → primary (AST)
    assert comp.index_file("a.py", "x", "python").symbols[0].name == "PRIMARY"
    # csharp → regex fallback
    fi = comp.index_file("A.cs", "public class Foo { }\n", "csharp")
    assert any(s.name == "Foo" for s in fi.symbols)


# ── service layer: unsupported language is an actionable error, not a silent 0 ───

def test_index_repo_rejects_unsupported_language(tmp_path):
    from engraphis.service import MemoryService, ValidationError
    svc = MemoryService.create(":memory:")
    (tmp_path / "a.py").write_text("def a(): pass\n")
    with pytest.raises(ValidationError):
        svc.index_repo(workspace="w", repo="r", root_path=str(tmp_path), languages=["rust"])


def test_index_repo_accepts_normalized_language_alias(tmp_path):
    from engraphis.service import MemoryService
    svc = MemoryService.create(":memory:")
    (tmp_path / "Svc.cs").write_text("public class Svc { }\n")
    res = svc.index_repo(workspace="w", repo="r", root_path=str(tmp_path), languages=["C#"])
    assert res["files_indexed"] >= 1 and res["symbols"] >= 1


# ── tree-sitter-specific behavior (skipped if the optional extra isn't installed) ──
#
# NB: this must be a *per-test* skip, not a module-level ``pytest.importorskip`` — the
# latter skips the ENTIRE module when tree-sitter is absent, which silently dropped all
# the dependency-free regex/ignore tests above from the offline numpy-only gate (exactly
# the environment CI runs in). Guard only the AST tests so the offline ones always run.
try:  # pragma: no cover - trivial availability probe
    import tree_sitter_language_pack  # noqa: F401
    _HAS_TREE_SITTER = True
except Exception:
    _HAS_TREE_SITTER = False

_needs_tree_sitter = pytest.mark.skipif(
    not _HAS_TREE_SITTER, reason="optional code-graph extra (tree-sitter) not installed")


@_needs_tree_sitter
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


@_needs_tree_sitter
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
