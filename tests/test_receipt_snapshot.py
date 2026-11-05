"""Receipt-chain verification reads rows and anchor from one snapshot."""
from __future__ import annotations

from engraphis.core.store import Store, verify_receipt_chain


class _TracingConnection:
    def __init__(self, conn):
        self._conn = conn
        self.statements: list[str] = []

    def execute(self, sql, *args, **kwargs):
        self.statements.append(str(sql).strip().upper())
        return self._conn.execute(sql, *args, **kwargs)

    def rollback(self):
        self.statements.append("ROLLBACK")
        return self._conn.rollback()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _store_with_chain() -> tuple[Store, str]:
    store = Store(":memory:")
    workspace_id = store.get_or_create_workspace("snapshot")
    store.record_receipt("remember", workspace_id=workspace_id)
    return store, workspace_id


def test_verify_receipt_chain_owns_a_read_transaction() -> None:
    store, workspace_id = _store_with_chain()
    trace = _TracingConnection(store.conn)
    try:
        result = verify_receipt_chain(trace, workspace_id=workspace_id)
    finally:
        store.close()
    assert result["valid"] is True
    assert trace.statements[0] == "BEGIN"
    assert trace.statements[-1] == "ROLLBACK"


def test_verify_receipt_chain_preserves_a_caller_owned_transaction() -> None:
    store, workspace_id = _store_with_chain()
    store.conn.execute("BEGIN IMMEDIATE")
    trace = _TracingConnection(store.conn)
    try:
        result = verify_receipt_chain(trace, workspace_id=workspace_id)
        assert result["valid"] is True
        assert store.conn.transaction_owned_by_current_thread() is True
        assert "ROLLBACK" not in trace.statements
    finally:
        store.conn.rollback()
        store.close()
