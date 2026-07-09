"""Code-symbol graph extraction — the flagship coding-agent wedge.

Populates the ``symbols``/``code_edges`` tables (already in ``core/schema.py``, unused
until now) by parsing source files into definitions (functions/methods/classes) and
best-effort ``calls``/``imports`` edges. Two backends, same shape as every other
pluggable piece in this codebase:

* ``TreeSitterSymbolIndexer`` — real AST parsing via ``tree-sitter`` (when installed).
  AST-derived structure is the source of truth for code relationships (more reliable
  than LLM extraction for this — AGENTS.md §3.8).
* ``RegexSymbolIndexer`` — dependency-free offline fallback. Flatter (no qualified
  names, no call edges) but always available, so a fresh clone with just ``numpy``
  installed still gets *something* out of ``index_repo`` rather than nothing.

``get_code_indexer()`` picks the best available backend, exactly like
``get_embedder``/``get_vector_index``/``get_reranker``. Keep heavy imports
(``tree_sitter*``) inside the try block — never at module level — so importing this
module never requires the optional dependency (AGENTS.md §3.8).

Note on the tree-sitter Python binding: recent releases (0.22+) changed several
``Node``/``Tree`` accessors from properties to methods (e.g. ``node.kind`` vs the
older ``node.type``) and the exact set varies by installed version. ``_call_or_get``
below tries the call form and falls back to plain attribute access so this module
works across that churn instead of pinning to one binding generation.

Note on str vs bytes: ``Parser.parse()`` disagrees on its source type across
binding generations — some accept only ``bytes`` (the byte-offset contract),
others only ``str`` (raising ``TypeError`` when given bytes). ``_parse`` below
tries bytes first and falls back to ``str`` so this module works across that
churn instead of pinning to one form. Node byte offsets (``start_byte``/
``end_byte``) are offsets into the UTF-8 bytes regardless of which form the
binding consumed, so ``TreeSitterSymbolIndexer`` encodes file content once in
``index_file`` and threads the ``bytes`` buffer through ``_walk``/``_text`` as
``src``, decoding back to ``str`` only at ``_text()`` where a symbol's slice is
extracted. Do not reintroduce a bare ``str`` "src" threaded into the walker —
``_text`` slices by byte offset and must slice a ``bytes`` buffer. Feeding a
``str`` to a bytes-only binding silently fails to parse (caught by
``engine.py``'s per-file ``except Exception: continue``), so ``index_repo``/
``search_code`` quietly return zero results instead of raising; that
regression shipped undetected for a while. See ``tests/test_codegraph.py``'s
tree-sitter cases and ``tests/test_engine.py::test_index_repo_and_search_code``
for the coverage that now guards it.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
}

# Per-language AST node kinds (tree-sitter grammars are consistent on these names).
_DEF_KINDS = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method"},
    "typescript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method"},
}
_CALL_KINDS = {"python": {"call"}, "javascript": {"call_expression"},
              "typescript": {"call_expression"}}
_IMPORT_KINDS = {
    "python": {"import_statement", "import_from_statement"},
    "javascript": {"import_statement"}, "typescript": {"import_statement"},
}
_CLASS_KINDS = {"python": {"class_definition"},
               "javascript": {"class_declaration"}, "typescript": {"class_declaration"}}


@dataclass
class Symbol:
    kind: str
    name: str
    fqname: str
    file: str
    span: str
    signature: str = ""
    lang: str = ""
    exported: bool = False
    content_hash: str = ""


@dataclass
class CodeEdge:
    src: str
    dst: str
    relation: str
    file: str = ""
    line: int = 0


@dataclass
class FileIndex:
    symbols: list[Symbol] = field(default_factory=list)
    edges: list[CodeEdge] = field(default_factory=list)


def detect_lang(file_path: str) -> Optional[str]:
    return LANG_BY_EXT.get(Path(file_path).suffix.lower())


def _content_hash(content: str) -> str:
    return hashlib.sha1(content.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ── tree-sitter backend ────────────────────────────────────────────────────────

class TreeSitterSymbolIndexer:
    """AST-based extraction via ``tree-sitter`` (optional dependency)."""

    def __init__(self) -> None:
        import tree_sitter_language_pack as _tslp  # lazy: optional dependency
        self._get_parser = _tslp.get_parser

    def supports(self, lang: str) -> bool:
        return lang in _DEF_KINDS

    def index_file(self, file_path: str, content: str, lang: str) -> FileIndex:
        parser = self._get_parser(lang)
        # Node.start_byte/end_byte are byte offsets, so we keep the bytes
        # buffer as `src` throughout and decode only at the point of text
        # extraction (see _text()).
        content_bytes = content.encode("utf-8", errors="replace")
        tree = _parse(parser, content_bytes)
        root = _cg(tree, "root_node")
        out = FileIndex()
        self._walk(root, content_bytes, file_path, lang, out, class_stack=[])
        return out

    def _walk(self, node, src: bytes, file_path: str, lang: str, out: FileIndex,
             *, class_stack: list[str]) -> None:
        kind = _node_kind(node)
        def_kinds = _DEF_KINDS.get(lang, {})
        if kind in def_kinds:
            name = self._def_name(node, src)
            if name:
                fqname = ".".join(class_stack + [name])
                symbol_kind = "method" if class_stack else def_kinds[kind]
                out.symbols.append(Symbol(
                    kind=symbol_kind, name=name, fqname=fqname, file=file_path,
                    span=f"{_start_line(node)}-{_end_line(node)}",
                    signature=_first_line(src, node), lang=lang,
                    exported=not name.startswith("_"),
                    content_hash=_content_hash(_text(src, node)),
                ))
            if kind in _CLASS_KINDS.get(lang, set()) and name:
                class_stack = class_stack + [name]
        elif kind in _CALL_KINDS.get(lang, set()):
            callee = self._call_target(node, src)
            if callee:
                caller = class_stack[-1] if class_stack else "<module>"
                out.edges.append(CodeEdge(src=caller, dst=callee, relation="calls",
                                          file=file_path, line=_start_line(node)))
        elif kind in _IMPORT_KINDS.get(lang, set()):
            for mod in self._import_targets(node, src):
                out.edges.append(CodeEdge(src=file_path, dst=mod, relation="imports",
                                          file=file_path, line=_start_line(node)))

        cc = _cg(node, "child_count")
        for i in range(cc):
            self._walk(_cg(node, "child", i), src, file_path, lang, out,
                      class_stack=class_stack)

    @staticmethod
    def _def_name(node, src: bytes) -> str:
        cc = _cg(node, "child_count")
        for i in range(cc):
            child = _cg(node, "child", i)
            if _node_kind(child) in ("identifier", "type_identifier", "property_identifier"):
                return _text(src, child)
        return ""

    @staticmethod
    def _call_target(node, src: bytes) -> str:
        cc = _cg(node, "child_count")
        if cc == 0:
            return ""
        first = _cg(node, "child", 0)
        fkind = _node_kind(first)
        if fkind == "identifier":
            return _text(src, first)
        if fkind in ("attribute", "member_expression"):
            # best-effort: use the last identifier-ish segment (obj.method -> method)
            fcc = _cg(first, "child_count")
            for j in range(fcc - 1, -1, -1):
                seg = _cg(first, "child", j)
                if _node_kind(seg) in ("identifier", "property_identifier"):
                    return _text(src, seg)
        return ""

    @staticmethod
    def _import_targets(node, src: bytes) -> list[str]:
        names = []
        cc = _cg(node, "child_count")
        for i in range(cc):
            child = _cg(node, "child", i)
            if _node_kind(child) in ("dotted_name", "identifier", "string"):
                text = _text(src, child).strip("\"'")
                if text:
                    names.append(text)
        return names[:1]  # the module path is conventionally the first dotted_name/string


def _cg(obj: Any, name: str, *args: Any) -> Any:
    """Call-or-get: tree-sitter's Python binding has changed several Node/Tree
    accessors between property and method across versions; try both so this module
    isn't pinned to one generation (see module docstring)."""
    val = getattr(obj, name)
    return val(*args) if callable(val) else val


def _node_kind(node: Any) -> str:
    """Return a node's grammar symbol name (e.g. ``"function_definition"``).

    Every released tree-sitter Python binding exposes this as the ``type``
    attribute (never ``kind`` -- that name doesn't exist on ``Node`` in any
    version we've checked, despite what the module docstring's version-churn
    note might suggest). Prefer ``type`` and only fall back to ``_cg(node,
    "kind")`` in case some future binding really does rename it.
    """
    if hasattr(node, "type"):
        val = node.type
        return val() if callable(val) else val
    return _cg(node, "kind")


def _text(src: bytes, node: Any) -> str:
    return src[_cg(node, "start_byte"):_cg(node, "end_byte")].decode("utf-8", errors="replace")


def _parse(parser: Any, content_bytes: bytes) -> Any:
    """Parse ``content_bytes`` across tree-sitter binding generations.

    Bindings disagree on ``Parser.parse()``'s source type: some accept only
    ``bytes`` (the byte-offset contract), others only ``str`` (raising
    ``TypeError: 'bytes' object is not an instance of 'str'`` when given bytes).
    Try bytes first, fall back to the str form. Node byte offsets are valid
    against either form (they index the UTF-8 bytes), so the bytes ``src``
    buffer stays correct regardless of which form the binding consumed.
    """
    try:
        return _cg(parser, "parse", content_bytes)
    except TypeError:
        return _cg(parser, "parse", content_bytes.decode("utf-8", errors="replace"))


def _row(pos: Any) -> int:
    if isinstance(pos, tuple):
        return pos[0]
    return getattr(pos, "row", 0)


def _start_line(node: Any) -> int:
    for attr in ("start_position", "start_point"):
        if hasattr(node, attr):
            return _row(_cg(node, attr)) + 1
    return 0


def _end_line(node: Any) -> int:
    for attr in ("end_position", "end_point"):
        if hasattr(node, attr):
            return _row(_cg(node, attr)) + 1
    return 0


def _first_line(src: bytes, node: Any) -> str:
    text = _text(src, node)
    return text.splitlines()[0].strip()[:200] if text else ""


# ── regex backend (offline, dependency-free fallback) ──────────────────────────

class RegexSymbolIndexer:
    """Dependency-free fallback: flat function/class detection, no qualified names
    or call edges. Always available — keeps ``index_repo`` useful with just ``numpy``
    installed (AGENTS.md §3.8: the core must work with no heavy dependencies)."""

    _PATTERNS = {
        "python": [
            (re.compile(r"^\s*def\s+(\w+)\s*\("), "function"),
            (re.compile(r"^\s*class\s+(\w+)"), "class"),
        ],
        "javascript": [
            (re.compile(r"^\s*(?:export\s+)?function\s+(\w+)\s*\("), "function"),
            (re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class"),
            (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
             "function"),
        ],
    }
    _PATTERNS["typescript"] = _PATTERNS["javascript"]

    def supports(self, lang: str) -> bool:
        return lang in self._PATTERNS

    def index_file(self, file_path: str, content: str, lang: str) -> FileIndex:
        out = FileIndex()
        patterns = self._PATTERNS.get(lang, [])
        for lineno, line in enumerate(content.splitlines(), start=1):
            for pattern, kind in patterns:
                m = pattern.match(line)
                if m:
                    name = m.group(1)
                    out.symbols.append(Symbol(
                        kind=kind, name=name, fqname=name, file=file_path,
                        span=f"{lineno}-{lineno}", signature=line.strip()[:200], lang=lang,
                        exported=not name.startswith("_"),
                        content_hash=_content_hash(line),
                    ))
        return out


def get_code_indexer(prefer: str = "auto"):
    """Return a tree-sitter indexer if available, else the regex fallback.

    ``prefer``: "auto" (try tree-sitter, fall back), "tree-sitter" (require it),
    or "regex" (force the dependency-free fallback).
    """
    if prefer == "regex":
        return RegexSymbolIndexer()
    try:
        return TreeSitterSymbolIndexer()
    except Exception:
        if prefer == "tree-sitter":
            raise
        return RegexSymbolIndexer()


def iter_source_files(root: str, *, exclude_dirs: Optional[set] = None) -> Iterable[str]:
    """Yield source file paths under ``root`` whose extension we know how to index."""
    exclude = exclude_dirs or {".git", "node_modules", "__pycache__", ".venv", "venv",
                               "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}
    base = Path(root)
    for path in base.rglob("*"):
        if not path.is_file() or detect_lang(str(path)) is None:
            continue
        if any(part in exclude for part in path.parts):
            continue
        yield str(path)
