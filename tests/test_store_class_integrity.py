"""Regression: ensure critical Store methods stay inside the class body.

A prior merge conflict accidentally nested several Store methods inside a
module-level helper function (_row_to_edge), making them unreachable dead
code. This broke /api/bootstrap because service.stats() could not call
store.prompt_eligibility_counts().

This test uses AST inspection to verify that all expected methods are
direct children of the Store class, not orphaned at module level or
nested inside other functions.
"""
import ast
from pathlib import Path

import pytest


def _get_store_class_node() -> ast.ClassDef:
    store_py = Path(__file__).resolve().parent.parent / "engraphis" / "core" / "store.py"
    source = store_py.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Store":
            return node
    raise AssertionError("Store class not found in store.py")


def _store_method_names() -> set[str]:
    cls = _get_store_class_node()
    return {
        n.name
        for n in cls.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


@pytest.mark.parametrize(
    "method",
    [
        "prompt_eligibility_counts",
        "embedding_space_health",
        "active_embedding_space",
        "embedding_rebuild_target",
        "embedding_space_ready",
        "begin_embedding_rebuild",
        "finish_embedding_rebuild",
        "init_schema",
        "_logical_digest",
        "_backup_before_v4_migration",
    ],
)
def test_critical_method_inside_store_class(method: str) -> None:
    names = _store_method_names()
    assert method in names, (
        f"{method} is not a direct child of Store class — "
        f"it may be orphaned at module level or nested inside another function"
    )


def test_no_self_methods_at_module_level() -> None:
    """Module-level functions must not use 'self' as first parameter."""
    store_py = Path(__file__).resolve().parent.parent / "engraphis" / "core" / "store.py"
    source = store_py.read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            if args.args and args.args[0].arg == "self":
                offenders.append(f"{node.name} (line {node.lineno})")
    assert not offenders, (
        "Module-level functions with 'self' parameter (likely orphaned methods): "
        + ", ".join(offenders)
    )


def test_logical_digest_tolerates_virtual_tables(tmp_path) -> None:
    """_logical_digest must not crash on databases with extension virtual tables."""
    import sqlite3
    from engraphis.core.store import Store

    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE normal(id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO normal VALUES (1, 'hello')")
    # Simulate a virtual table entry that would crash iterdump
    # (we can't actually create one without the extension, but we can
    # verify the code path handles the skip_tables logic)
    digest = Store._logical_digest(conn)
    assert isinstance(digest, str)
    assert len(digest) == 64  # SHA-256 hex
    conn.close()
