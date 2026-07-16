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

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

LANG_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".c": "c", ".h": "c",
    ".sql": "sql",
    ".tf": "terraform", ".tfvars": "terraform",
}

# Human-typed language names (from the ``languages=`` filter) → the canonical id used
# in LANG_BY_EXT. Lets a caller pass "C#", "cpp", "py" etc. and get a useful answer
# instead of a silent no-op. Anything not here is treated as-is (and then validated).
_LANG_ALIASES = {
    "py": "python", "python": "python",
    "js": "javascript", "javascript": "javascript", "node": "javascript",
    "ts": "typescript", "typescript": "typescript",
    "go": "go", "golang": "go",
    "rs": "rust", "rust": "rust",
    "java": "java",
    "cs": "csharp", "c#": "csharp", "csharp": "csharp",
    "c": "c", "h": "c",
    "c++": "cpp", "cpp": "cpp", "cplusplus": "cpp", "cxx": "cpp", "hpp": "cpp",
    "sql": "sql",
    "tf": "terraform", "hcl": "terraform", "terraform": "terraform",
}


def normalize_language(name: str) -> str:
    """Fold a user-typed language name to its canonical id ('C#' -> 'csharp')."""
    key = (name or "").strip().lower()
    return _LANG_ALIASES.get(key, key)


def supported_languages() -> set:
    """The set of canonical language ids ``index_repo`` can extract symbols for.

    Sourced from ``LANG_BY_EXT`` so it can never drift from what actually gets indexed.
    Used to reject an unknown ``languages=`` filter with an actionable error instead of
    walking the whole tree and silently returning zero symbols.
    """
    return set(LANG_BY_EXT.values())

# Per-language AST node kinds (tree-sitter grammars are consistent on these names).
_DEF_KINDS = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "javascript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method"},
    "typescript": {"function_declaration": "function", "class_declaration": "class",
                   "method_definition": "method", "interface_declaration": "interface"},
    "go": {"function_declaration": "function", "method_declaration": "method",
           "type_spec": "type"},
    "rust": {"function_item": "function", "struct_item": "class", "enum_item": "class",
             "trait_item": "interface"},
    "java": {"class_declaration": "class", "interface_declaration": "interface",
             "enum_declaration": "class", "record_declaration": "class",
             "method_declaration": "method", "constructor_declaration": "method"},
}
_CALL_KINDS = {"python": {"call"}, "javascript": {"call_expression"},
              "typescript": {"call_expression"}, "go": {"call_expression"},
              "rust": {"call_expression"}, "java": {"method_invocation",
                                                     "object_creation_expression"}}
_IMPORT_KINDS = {
    "python": {"import_statement", "import_from_statement"},
    "javascript": {"import_statement"}, "typescript": {"import_statement"},
    "go": {"import_declaration"}, "rust": {"use_declaration"},
    "java": {"import_declaration"},
}
_VAR_KINDS = {
    "python": {"assignment", "annotated_assignment"},
    "javascript": {"variable_declarator", "field_definition"},
    "typescript": {"variable_declarator", "public_field_definition",
                   "required_parameter", "optional_parameter"},
    "go": {"var_spec", "const_spec"},
    "rust": {"const_item", "static_item"},
    "java": {"variable_declarator"},
}
_CLASS_KINDS = {"python": {"class_definition"},
               "javascript": {"class_declaration"},
               "typescript": {"class_declaration", "interface_declaration"},
               "go": {"type_spec"},
               "rust": {"struct_item", "enum_item", "trait_item"},
               "java": {"class_declaration", "interface_declaration", "enum_declaration",
                        "record_declaration"}}


@dataclass
class Symbol:
    kind: str
    name: str
    fqname: str
    file: str
    span: str
    signature: str = ""
    docstring: str = ""
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
        self._walk(root, content_bytes, file_path, lang, out, scope_stack=[])
        return out

    def _walk(self, node, src: bytes, file_path: str, lang: str, out: FileIndex,
             *, scope_stack: list[tuple[str, str]]) -> None:
        kind = _node_kind(node)
        def_kinds = _DEF_KINDS.get(lang, {})
        next_scope = scope_stack
        if kind in def_kinds:
            name = self._def_name(node, src)
            if name:
                names = [part[0] for part in scope_stack]
                fqname = ".".join(names + [name])
                inside_class = any(part[1] in ("class", "interface", "type")
                                   for part in scope_stack)
                declared_kind = def_kinds[kind]
                symbol_kind = (
                    "method" if declared_kind == "method"
                    or (inside_class and declared_kind == "function")
                    else declared_kind
                )
                out.symbols.append(Symbol(
                    kind=symbol_kind, name=name, fqname=fqname, file=file_path,
                    span=f"{_start_line(node)}-{_end_line(node)}",
                    signature=_first_line(src, node),
                    docstring=_documentation(src, node, lang), lang=lang,
                    exported=not name.startswith("_"),
                    content_hash=_content_hash(_text(src, node)),
                ))
                parent = ".".join(names) if names else file_path
                out.edges.append(CodeEdge(
                    src=parent, dst=fqname, relation="defines",
                    file=file_path, line=_start_line(node),
                ))
                if kind in _CLASS_KINDS.get(lang, set()):
                    for base, relation in self._base_targets(node, src, lang):
                        out.edges.append(CodeEdge(
                            src=fqname, dst=base, relation=relation,
                            file=file_path, line=_start_line(node),
                        ))
                next_scope = scope_stack + [(name, symbol_kind)]
        elif kind in _VAR_KINDS.get(lang, set()) and not any(
            part[1] in ("function", "method") for part in scope_stack
        ):
            name = self._def_name(node, src)
            if name:
                names = [part[0] for part in scope_stack]
                fqname = ".".join(names + [name])
                out.symbols.append(Symbol(
                    kind="variable", name=name, fqname=fqname, file=file_path,
                    span=f"{_start_line(node)}-{_end_line(node)}",
                    signature=_first_line(src, node),
                    docstring=_documentation(src, node, lang), lang=lang,
                    exported=not name.startswith("_"),
                    content_hash=_content_hash(_text(src, node)),
                ))
                out.edges.append(CodeEdge(
                    src=".".join(names) if names else file_path,
                    dst=fqname, relation="defines", file=file_path,
                    line=_start_line(node),
                ))
        elif kind in _CALL_KINDS.get(lang, set()):
            callee = self._call_target(node, src)
            if callee:
                caller = (
                    ".".join(part[0] for part in scope_stack)
                    if scope_stack else file_path
                )
                out.edges.append(CodeEdge(src=caller, dst=callee, relation="calls",
                                          file=file_path, line=_start_line(node)))
        elif kind in _IMPORT_KINDS.get(lang, set()):
            for mod in self._import_targets(node, src):
                out.edges.append(CodeEdge(src=file_path, dst=mod, relation="imports",
                                          file=file_path, line=_start_line(node)))

        cc = _cg(node, "child_count")
        for i in range(cc):
            self._walk(_cg(node, "child", i), src, file_path, lang, out,
                      scope_stack=next_scope)

    @staticmethod
    def _def_name(node, src: bytes) -> str:
        if hasattr(node, "child_by_field_name"):
            try:
                named = _cg(node, "child_by_field_name", "name")
                if named is not None:
                    text = _text(src, named)
                    if text:
                        return text
            except Exception:
                pass
        cc = _cg(node, "child_count")
        for i in range(cc):
            child = _cg(node, "child", i)
            if _node_kind(child) in ("identifier", "type_identifier", "property_identifier"):
                return _text(src, child)
        return ""

    @staticmethod
    def _call_target(node, src: bytes) -> str:
        if hasattr(node, "child_by_field_name"):
            for field_name in ("function", "name", "type"):
                try:
                    target = _cg(node, "child_by_field_name", field_name)
                except Exception:
                    target = None
                if target is not None:
                    target_text = _text(src, target)
                    name = (
                        _first_identifier(target_text)
                        if field_name == "type"
                        else _last_identifier(target_text)
                    )
                    if name:
                        return name
        cc = _cg(node, "child_count")
        if cc == 0:
            return ""
        first = _cg(node, "child", 0)
        fkind = _node_kind(first)
        if fkind == "identifier":
            return _text(src, first)
        if fkind in (
            "attribute", "member_expression", "selector_expression", "field_expression",
            "scoped_identifier", "qualified_identifier",
        ):
            return _last_identifier(_text(src, first))
        return _last_identifier(_text(src, first))

    @staticmethod
    def _base_targets(node, src: bytes, lang: str) -> list[tuple[str, str]]:
        return _base_targets_from_signature(_first_line(src, node), lang)

    @staticmethod
    def _import_targets(node, src: bytes) -> list[str]:
        wanted = {
            "dotted_name", "string", "interpreted_string_literal", "raw_string_literal",
            "scoped_identifier", "qualified_identifier",
        }
        stack = [node]
        while stack:
            current = stack.pop()
            if current is not node and _node_kind(current) in wanted:
                text = _text(src, current).strip("\"'`")
                if text:
                    return [text]
            cc = _cg(current, "child_count")
            for i in range(cc - 1, -1, -1):
                stack.append(_cg(current, "child", i))
        return []


def _base_targets_from_signature(first: str, lang: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if lang == "python":
        match = re.search(r"\bclass\s+\w+\s*\(([^)]*)\)", first)
        if match:
            out.extend(
                (base.strip().split("[", 1)[0], "inherits")
                for base in match.group(1).split(",") if base.strip()
            )
    elif lang in {"javascript", "typescript"}:
        match = re.search(r"\bextends\s+([A-Za-z_$][\w.$]*)", first)
        if match:
            out.append((match.group(1), "inherits"))
        match = re.search(r"\bimplements\s+([^{]+)", first)
        if match:
            out.extend(
                (base.strip(), "implements")
                for base in match.group(1).split(",") if base.strip()
            )
    elif lang == "java":
        match = re.search(r"\bextends\s+([A-Za-z_$][\w.$<>]*)", first)
        if match:
            out.append((match.group(1).split("<", 1)[0], "inherits"))
        match = re.search(r"\bimplements\s+([^{]+)", first)
        if match:
            out.extend(
                (base.strip().split("<", 1)[0], "implements")
                for base in match.group(1).split(",") if base.strip()
            )
    return out[:20]


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


def _last_identifier(text: str) -> str:
    matches = re.findall(r"[A-Za-z_$][\w$]*", str(text or ""))
    return matches[-1] if matches else ""


def _first_identifier(text: str) -> str:
    match = re.search(r"[A-Za-z_$][\w$]*", str(text or ""))
    return match.group(0) if match else ""


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


def _documentation(src: bytes, node: Any, lang: str) -> str:
    """Best-effort docstring or immediately leading comment block."""
    node_text = _text(src, node)
    if lang == "python":
        match = re.search(
            r"^[^\n]*\n\s*(?:[rubfRUBF]*)('''|\"\"\")([\s\S]*?)\1",
            node_text,
        )
        if match:
            return " ".join(match.group(2).strip().split())[:2_000]
    lines = src.decode("utf-8", errors="replace").splitlines()
    return _leading_comment_from_lines(lines, max(0, _start_line(node) - 1))


def _leading_comment_from_lines(lines: list[str], index: int) -> str:
    collected: list[str] = []
    cursor = index - 1
    while cursor >= 0 and len(collected) < 20:
        stripped = lines[cursor].strip()
        if not stripped:
            if collected:
                break
            cursor -= 1
            continue
        if stripped.startswith(("#", "//", "///", "/*", "*", "--")):
            cleaned = re.sub(r"^(?:#|///?|/\*+|\*+|--)\s?", "", stripped)
            cleaned = cleaned.rstrip("*/ ").strip()
            if cleaned:
                collected.append(cleaned)
            cursor -= 1
            continue
        break
    return " ".join(reversed(collected))[:2_000]


# ── regex backend (offline, dependency-free fallback) ──────────────────────────

class RegexSymbolIndexer:
    """Dependency-free fallback: flat function/class detection, no qualified names
    or call edges. Always available — keeps ``index_repo`` useful with just ``numpy``
    installed (AGENTS.md §3.8: the core must work with no heavy dependencies)."""

    _PATTERNS = {
        "python": [
            (re.compile(r"^\s*def\s+(\w+)\s*\("), "function"),
            (re.compile(r"^\s*class\s+(\w+)"), "class"),
            (re.compile(r"^\s*([A-Z][A-Z0-9_]{2,})\s*(?::[^=]+)?="), "variable"),
        ],
        "javascript": [
            (re.compile(r"^\s*(?:export\s+)?function\s+(\w+)\s*\("), "function"),
            (re.compile(r"^\s*(?:export\s+)?class\s+(\w+)"), "class"),
            (re.compile(r"^\s*(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>"),
             "function"),
            (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(\w+)\b"), "variable"),
        ],
        "go": [
            (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(\w+)\s*\("), "function"),
            (re.compile(r"^\s*type\s+(\w+)\s+(?:struct|interface)\b"), "type"),
            (re.compile(r"^\s*(?:var|const)\s+(\w+)\b"), "variable"),
        ],
        "rust": [
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)\s*[<(]"),
             "function"),
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum)\s+(\w+)"), "class"),
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?trait\s+(\w+)"), "interface"),
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const|static)\s+(\w+)"),
             "variable"),
        ],
        "java": [
            (re.compile(
                r"^\s*(?:(?:public|private|protected|abstract|final|static|sealed|"
                r"non-sealed)\s+)*(?:class|interface|enum|record)\s+(\w+)"), "class"),
            (re.compile(
                r"^\s*(?:(?:public|private|protected|abstract|final|static|synchronized|"
                r"native|default)\s+)+(?:[\w<>\[\],.?]+\s+)+(\w+)\s*\("), "method"),
        ],
        # C#: type declarations (class/struct/interface/record/enum) and methods. The
        # method pattern requires ≥1 modifier + ≥1 return-type token before the name so
        # control-flow (`if (`, `while (`) and plain calls don't masquerade as definitions.
        "csharp": [
            (re.compile(
                r"^\s*(?:\[[^\]]*\]\s*)*"
                r"(?:(?:public|private|protected|internal|static|sealed|abstract|partial|"
                r"readonly|unsafe|new)\s+)*"
                r"(?:class|struct|interface|record|enum)\s+(\w+)"), "class"),
            (re.compile(
                r"^\s*(?:\[[^\]]*\]\s*)*"
                r"(?:(?:public|private|protected|internal|static|virtual|override|abstract|"
                r"async|sealed|extern|unsafe|new|partial)\s+){1,8}"
                r"(?:[\w<>\[\],\.\?]+\s+){1,6}(\w+)\s*\("), "method"),
        ],
        # C / C++: class/struct declarations and free/member function definitions.
        # Regex C++ is inherently best-effort (the AST backend is the real path); the
        # stop-name guard below drops the common false positives.
        "cpp": [
            (re.compile(r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(\w+)"), "class"),
            (re.compile(
                r"^\s*(?:[\w:<>\*&\[\]]+\s+){1,8}(?:\w+::)*([\w~]+)\s*\([^;{]*\)\s*"
                r"(?:const\b\s*)?(?:noexcept\b\s*)?(?:override\b\s*)?\{"), "function"),
        ],
        "sql": [
            (re.compile(
                r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|FUNCTION|PROCEDURE|"
                r"TRIGGER|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`[]?([\w.]+)",
                re.IGNORECASE), "definition"),
        ],
        "terraform": [
            (re.compile(
                r'^\s*(?:resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{'), "resource"),
            (re.compile(
                r'^\s*(module|variable|output|provider|locals)\s+"?([^"\s{]+)"?\s*\{'),
             "block"),
        ],
    }
    _PATTERNS["typescript"] = _PATTERNS["javascript"]
    _PATTERNS["c"] = _PATTERNS["cpp"]

    # Names a pattern might capture that are never real definitions (language keywords
    # that syntactically resemble a definition head). Keeps the coarse C-family and C#
    # patterns from emitting junk symbols.
    _STOPNAMES = {
        "cpp": {"if", "for", "while", "switch", "return", "sizeof", "catch", "else",
                "do", "case", "new", "delete", "throw", "using", "namespace", "template",
                "typedef", "struct", "class", "enum", "union", "operator", "static_assert"},
        "csharp": {"if", "for", "while", "switch", "return", "foreach", "using", "lock",
                   "catch", "fixed", "get", "set", "add", "remove", "yield", "when"},
    }
    _STOPNAMES["c"] = _STOPNAMES["cpp"]

    # Any single source line longer than this is skipped by the regex indexer.
    # Lines this long are pathological (crafted DoS inputs), not legitimate source
    # code. Also bounds the worst-case match-time for each compiled pattern.
    _MAX_LINE_LEN = 4096

    def supports(self, lang: str) -> bool:
        return lang in self._PATTERNS

    def index_file(self, file_path: str, content: str, lang: str) -> FileIndex:
        out = FileIndex()
        patterns = self._PATTERNS.get(lang, [])
        stop = self._STOPNAMES.get(lang, set())
        seen: set = set()  # (name, lineno) — one symbol per line even if patterns overlap
        lines = content.splitlines()
        for lineno, line in enumerate(lines, start=1):
            if len(line) > self._MAX_LINE_LEN:
                continue
            for pattern, kind in patterns:
                m = pattern.match(line)
                if not m:
                    continue
                if lang == "terraform" and (m.lastindex or 0) >= 2:
                    name = f"{m.group(1)}.{m.group(2)}"
                else:
                    name = m.group(1)
                if name in stop or (name, lineno) in seen:
                    continue
                seen.add((name, lineno))
                out.symbols.append(Symbol(
                    kind=kind, name=name, fqname=name, file=file_path,
                    span=f"{lineno}-{lineno}", signature=line.strip()[:200],
                    docstring=_leading_comment_from_lines(lines, lineno - 1), lang=lang,
                    exported=not name.startswith("_"),
                    content_hash=_content_hash(line),
                ))
                out.edges.append(CodeEdge(
                    src=file_path, dst=name, relation="defines",
                    file=file_path, line=lineno,
                ))
                if kind in {"class", "interface", "type"}:
                    for base, relation in _base_targets_from_signature(line, lang):
                        out.edges.append(CodeEdge(
                            src=name, dst=base, relation=relation,
                            file=file_path, line=lineno,
                        ))
        return out


class CompositeSymbolIndexer:
    """Route each language to the best backend that supports it: AST (tree-sitter)
    where it can, the dependency-free regex indexer otherwise.

    This is what lets us ship useful C#/C/C++ support today (regex-level: class/struct/
    method/function *definitions*, which is what powers ``search_code``) without the AST
    backend having grammar-specific node maps for them yet — and without regressing the
    high-quality AST extraction for Python/JS/TS. When AST maps for a language are added
    later, ``supports`` moves it to the primary automatically, no caller change needed.
    """

    def __init__(self, primary: Any, fallback: Any) -> None:
        self._primary = primary
        self._fallback = fallback

    def supports(self, lang: str) -> bool:
        return self._primary.supports(lang) or self._fallback.supports(lang)

    def index_file(self, file_path: str, content: str, lang: str) -> FileIndex:
        idx = self._primary if self._primary.supports(lang) else self._fallback
        return idx.index_file(file_path, content, lang)


def get_code_indexer(prefer: str = "auto"):
    """Return the best available code indexer.

    ``prefer``: "auto" (AST via tree-sitter where possible, regex fallback per-language),
    "tree-sitter" (require the AST backend, no regex fallback), or "regex" (force the
    dependency-free fallback for every language).
    """
    if prefer == "regex":
        return RegexSymbolIndexer()
    try:
        ts = TreeSitterSymbolIndexer()
    except Exception:
        if prefer == "tree-sitter":
            raise
        return RegexSymbolIndexer()
    if prefer == "tree-sitter":
        return ts
    return CompositeSymbolIndexer(ts, RegexSymbolIndexer())


# Build/generated/dependency directories skipped by default. Kept broad on purpose:
# these are exactly the big trees that made ``index_repo`` appear to *hang* on C#/C++/
# JVM repos — the old rglob("*") descended into every one of them before filtering.
_DEFAULT_EXCLUDE_DIRS = {
    ".git", ".hg", ".svn",
    "node_modules", "bower_components", "jspm_packages",
    "__pycache__", ".venv", "venv", "env", ".env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".nox",
    "dist", "build", "_build", "out", "target", "coverage", ".cache",
    # C# / C++ / JVM / Xcode / IDE generated output
    "bin", "obj", "packages", "vendor", "Pods", "DerivedData",
    ".gradle", ".idea", ".vs", ".vscode",
    "cmake-build-debug", "cmake-build-release",
    # JS framework build output
    ".next", ".nuxt", ".svelte-kit", ".angular",
}

IGNORE_FILENAME = ".engraphisignore"
_MAX_IGNORE_BYTES = 64 * 1024
_MAX_IGNORE_PATTERNS = 1_000
_MAX_IGNORE_PATTERN_LEN = 256  # per-line cap: fnmatch compiles to a backtracking regex,
                               # so one giant wildcard pattern is a ReDoS vector — bound it.


def load_ignore_patterns(root: str) -> tuple:
    """Parse ``<root>/.engraphisignore`` into ``(names, globs, unignore)``.

    gitignore-flavoured, deliberately small and DoS-bounded. The ignore file lives inside
    a possibly-untrusted repo, so every input is bounded: file size (``_MAX_IGNORE_BYTES``),
    total pattern count (``_MAX_IGNORE_PATTERNS``), and per-pattern length
    (``_MAX_IGNORE_PATTERN_LEN`` — fnmatch translates each glob to a backtracking ``re``
    pattern, so an unbounded wildcard string would be a ReDoS vector). Patterns only ever
    *prune* a walk already confined to ``root``; they can never widen it.

    * ``# comment`` and blank lines are ignored.
    * ``!name`` re-includes a name the ignore file itself excluded (gitignore-style). It
      can NOT re-expose a hardcoded default (``node_modules``/``.git``/build dirs …) —
      those stay excluded no matter what an untrusted ``.engraphisignore`` says, so it
      can't reintroduce the large-tree hang or pull vendored code into the graph.
    * a bare token with no wildcard (``fixtures``) matches that file/dir name anywhere.
    * a token with a wildcard or slash (``*.gen.cs``, ``src/generated/*``) is a glob
      matched against each candidate's repo-root-relative POSIX path (and basename).

    Returns empty sets/lists when there is no readable ignore file.
    """
    names: set = set()
    globs: list = []
    unignore: set = set()
    path = Path(root) / IGNORE_FILENAME
    try:
        if not path.is_file() or path.stat().st_size > _MAX_IGNORE_BYTES:
            return names, globs, unignore
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return names, globs, unignore
    for raw in text.splitlines():
        if len(names) + len(globs) + len(unignore) >= _MAX_IGNORE_PATTERNS:
            break
        line = raw.strip()
        if not line or line.startswith("#") or len(line) > _MAX_IGNORE_PATTERN_LEN:
            continue
        if line.startswith("!"):
            tok = line[1:].strip().strip("/")
            if tok and not _has_glob(tok):
                unignore.add(tok)
            continue
        line = line.rstrip("/")
        if _has_glob(line) or "/" in line:
            globs.append(line.strip("/"))
        else:
            names.add(line)
    return names, globs, unignore


def _has_glob(s: str) -> bool:
    return any(c in s for c in "*?[")


def _rel_posix(rel_dir: str, name: str) -> str:
    if rel_dir in ("", "."):
        return name
    return rel_dir.replace(os.sep, "/") + "/" + name


# Upper bound on directories visited in a single walk. Pairs with the engine's
# ``max_files`` cap: stops a pathological tree (millions of empty dirs) from spinning
# even when few files are ever yielded.
_MAX_WALK_DIRS = 200_000


class SourceWalkLimitExceeded(RuntimeError):
    """The bounded repository walk stopped before visiting the full tree."""


def iter_source_files(root: str, *, exclude_dirs: Optional[set] = None,
                      respect_ignore_file: bool = True) -> Iterable[str]:
    """Yield indexable source-file paths under ``root``.

    Prunes excluded directories *during* the walk (``os.walk`` with in-place ``dirnames``
    filtering) so it never descends huge build/dependency trees — the fix for the
    apparent hang on large non-Python repos — and never follows symlinks (``followlinks=
    False`` for dirs; per-file ``islink`` skip for files) so it can neither loop on a
    symlink cycle nor read a file that points *outside* ``root`` (e.g. a repo shipping
    ``leak.py -> /etc/passwd``). ``.engraphisignore`` at the repo root adds project-
    specific ignores; pass ``respect_ignore_file=False`` to skip reading it.
    """
    base = Path(root)
    root_str = str(base)
    default_excl = set(exclude_dirs) if exclude_dirs is not None else set(_DEFAULT_EXCLUDE_DIRS)
    ig_names: set = set()
    ig_globs: list = []
    unignore: set = set()
    if respect_ignore_file:
        ig_names, ig_globs, unignore = load_ignore_patterns(root_str)
    # Defaults are non-negotiable: `!` can only re-include a name the ignore file itself
    # added, never a hardcoded default — an untrusted repo can't disable the hang guards.
    excl_dir_names = default_excl | (ig_names - unignore)

    def _glob_hit(rel_path: str, name: str) -> bool:
        return any(fnmatch.fnmatch(rel_path, g) or fnmatch.fnmatch(name, g) for g in ig_globs)

    dirs_seen = 0
    for dirpath, dirnames, filenames in os.walk(root_str, followlinks=False):
        dirs_seen += 1
        if dirs_seen > _MAX_WALK_DIRS:
            raise SourceWalkLimitExceeded(
                f"repository walk exceeded {_MAX_WALK_DIRS} directories"
            )
        rel_dir = os.path.relpath(dirpath, root_str)
        # prune in place so os.walk skips these subtrees entirely
        dirnames[:] = [
            d for d in dirnames
            if d not in excl_dir_names and not _glob_hit(_rel_posix(rel_dir, d), d)
        ]
        for fn in filenames:
            if detect_lang(fn) is None or fn in ig_names:
                continue
            if _glob_hit(_rel_posix(rel_dir, fn), fn):
                continue
            full = os.path.join(dirpath, fn)
            if os.path.islink(full):  # never read a symlink target (may escape root)
                continue
            yield full
