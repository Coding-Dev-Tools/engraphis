"""Event-scheduled regressions in disposable child processes, with original timeouts."""

import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest


def _network_denied(event, _args):
    if event in {
        "socket.connect",
        "socket.connect_ex",
        "socket.bind",
        "socket.getaddrinfo",
    }:
        raise RuntimeError("Regression child denies network")


class _ObservedEngineLock:
    def __init__(self, original, second_attempt):
        self.original, self.second_attempt = original, second_attempt
        self.first_held = False

    def acquire(self, *args, **kwargs):
        name = threading.current_thread().name
        if name == "second":
            self.second_attempt.set()
        acquired = self.original.acquire(*args, **kwargs)
        if name == "first" and acquired:
            self.first_held = True
        return acquired

    def release(self):
        self.original.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_):
        self.release()


def _snapshot(service):
    conn = service.store.conn
    return {
        "memories": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
        "receipts": conn.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0],
        "heads": [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM receipt_chain_heads ORDER BY workspace_id"
            ).fetchall()
        ],
    }


def _child(case, directory):
    sys.addaudithook(_network_denied)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from engraphis.service import MemoryService
    from engraphis.core.store import _SerializedConnection
    from engraphis.core.interfaces import FactSpec

    assert _SerializedConnection._ACQUIRE_TIMEOUT == 60.0
    service = MemoryService.create(
        str(Path(directory) / "probe.db"),
        embed_model="",
        vector_backend="numpy",
        extractor="none",
        graph_extractor="none",
        retention_supervisor="none",
        allow_automatic_critical_retention=False,
        sqlite_durability="durable",
        require_exact_backends=True,
    )
    initial = service.remember(
        "Synthetic concurrency anchor is cobalt.",
        workspace="audit",
        trusted=True,
        resolve_conflicts=False,
    )
    conn = service.store.conn
    wid = service.store.get_or_create_workspace("audit")
    conn.execute("CREATE TABLE concurrency_probe_marker(value TEXT NOT NULL)")
    conn.commit()
    baseline = _snapshot(service)

    def batch():
        return service.remember_batch(
            [
                {
                    "content": "Synthetic batch marker is indigo.",
                    "trusted": True,
                    "resolve_conflicts": False,
                }
            ],
            workspace="audit",
        )

    def ordinary():
        return service.remember(
            "Synthetic ordinary marker is amber.",
            workspace="audit",
            trusted=True,
            resolve_conflicts=False,
        )

    if case == "reentrant":
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO concurrency_probe_marker VALUES ('caller')")
        with service.engine._write_lock:
            batch()
            service.engine.remember(
                "Synthetic nested marker is azure.",
                workspace_id=wid,
                resolve_conflicts=False,
            )
        assert conn.transaction_owned_by_current_thread()
        assert conn.execute("SELECT COUNT(*) FROM concurrency_probe_marker").fetchone()[0] == 1
        conn.rollback()
        assert _snapshot(service) == baseline
        service.close()
        return {
            "status": "PASS",
            "caller_transaction_preserved_and_rolled_back_by_caller": True,
        }
    if case.startswith("receipt_atomic_"):
        conn.execute(
            "CREATE TRIGGER refuse_receipt_anchor BEFORE INSERT ON receipt_chain_heads BEGIN SELECT RAISE(ABORT, 'synthetic anchor refusal'); END"
        )
        conn.commit()
        caller_owned = case.endswith("caller")
        if caller_owned:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO concurrency_probe_marker VALUES ('caller')")
        try:
            service.store.record_receipt("recall", workspace_id=wid)
        except Exception:
            pass
        else:
            raise AssertionError("Injected anchor failure did not raise")
        assert conn.transaction_owned_by_current_thread() is caller_owned
        assert _snapshot(service) == baseline, (
            "Receipt payload/anchor append must roll back together"
        )
        if caller_owned:
            assert conn.execute("SELECT COUNT(*) FROM concurrency_probe_marker").fetchone()[0] == 1
            conn.rollback()
        service.close()
        return {
            "status": "PASS",
            "receipt_and_anchor_rollback_atomic": True,
            "caller_owned": caller_owned,
        }
    first_at_sql, second_attempt = threading.Event(), threading.Event()
    errors, completed, preserved = [], [], []
    original_acquire, original_receipt = conn._acquire, service.store.record_receipt
    observed_engine = _ObservedEngineLock(service.engine._write_lock, second_attempt)
    service.engine._write_lock = observed_engine
    receipt_entered = threading.local()
    paused = False

    def receipt(*args, **kwargs):
        if threading.current_thread().name == "first":
            receipt_entered.active = True
        elif case == "receipt" and threading.current_thread().name == "second":
            assert conn.transaction_owned_by_current_thread()
            second_attempt.set()
        return original_receipt(*args, **kwargs)

    def acquire():
        nonlocal paused
        inside = (
            getattr(receipt_entered, "active", False)
            if case == "receipt"
            else observed_engine.first_held
        )
        if threading.current_thread().name == "first" and inside and not paused:
            paused = True
            first_at_sql.set()
            assert second_attempt.wait(5), "Second API did not reach its original acquisition point"
        original_acquire()

    object.__setattr__(conn, "_acquire", acquire)
    service.store.record_receipt = receipt

    def second_action():
        if not case.startswith("caller_"):
            return batch()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO concurrency_probe_marker VALUES ('caller')")
        before = _snapshot(service)
        try:
            if case == "caller_remember":
                service.engine.remember(
                    "Synthetic direct caller memory.",
                    workspace_id=wid,
                    resolve_conflicts=False,
                )
            elif case == "caller_many":
                service.engine.remember_many(
                    [FactSpec(content="Synthetic direct caller batch.")],
                    workspace_id=wid,
                )
            elif case == "caller_erase":
                service.engine.secure_erase(initial["id"])
            else:
                batch()
        except RuntimeError as error:
            assert (
                str(error)
                == "engine writer is busy; settle the caller-owned transaction before retrying"
            )
        else:
            raise AssertionError("Conflicting caller-owned engine admission must fail")
        assert conn.transaction_owned_by_current_thread()
        assert _snapshot(service) == before
        assert conn.execute("SELECT COUNT(*) FROM concurrency_probe_marker").fetchone()[0] == 1
        preserved.append(True)
        conn.rollback()

    def invoke(name, action):
        try:
            action()
            completed.append(name)
        except BaseException as error:
            errors.append({"thread": name, "type": type(error).__name__, "message": str(error)})

    first_action = (
        (lambda: service.recall("synthetic anchor", workspace="audit", k=3, reinforce=False))
        if case == "receipt"
        else ordinary
    )
    first = threading.Thread(name="first", target=invoke, args=("first", first_action), daemon=True)
    second = threading.Thread(
        name="second", target=invoke, args=("second", second_action), daemon=True
    )
    first.start()
    assert first_at_sql.wait(5), "First API did not enter its original SQLite acquisition point"
    second.start()
    first.join(5)
    second.join(5)
    if first.is_alive() or second.is_alive():
        return {
            "status": "BLOCKED_ORIGINAL_LOCK_ORDER",
            "case": case,
            "completed": completed,
            "errors": errors,
            "original_sql_timeout": 60.0,
        }
    assert not errors, errors
    assert set(completed) == {"first", "second"}
    if case.startswith("caller_"):
        assert preserved == [True]
        assert conn.execute("SELECT COUNT(*) FROM concurrency_probe_marker").fetchone()[0] == 0
    assert service.store.verify_receipts(workspace_id=wid)["valid"] is True
    object.__setattr__(conn, "_acquire", original_acquire)
    service.store.record_receipt = original_receipt
    service.engine._write_lock = observed_engine.original
    service.close()
    return {
        "status": "PASS",
        "case": case,
        "receipt_chain_valid": True,
        "caller_preserved": bool(preserved),
        "original_sql_timeout": 60.0,
    }


CASES = (
    "receipt",
    "service_write",
    "caller_remember",
    "caller_many",
    "caller_erase",
    "caller_service_batch",
    "reentrant",
    "receipt_atomic_caller",
    "receipt_atomic_standalone",
)


@pytest.mark.parametrize("case", CASES)
def test_no_lock_inversion_or_caller_transaction_loss(tmp_path, case):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT"}
    }
    for directory in ("home", "state", "tmp", "appdata", "hf"):
        (tmp_path / directory).mkdir()
    environment.update(
        HOME=str(tmp_path / "home"),
        USERPROFILE=str(tmp_path / "home"),
        APPDATA=str(tmp_path / "appdata"),
        LOCALAPPDATA=str(tmp_path / "appdata"),
        TEMP=str(tmp_path / "tmp"),
        TMP=str(tmp_path / "tmp"),
        ENGRAPHIS_STATE_DIR=str(tmp_path / "state"),
        ENGRAPHIS_DB_PATH=str(tmp_path / "probe.db"),
        ENGRAPHIS_EMBED_MODEL="",
        ENGRAPHIS_UPDATE_CHECK="0",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_HOME=str(tmp_path / "hf"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        str(Path(__file__).resolve()),
        "--child",
        case,
        str(tmp_path),
    ]
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "PASS"


if __name__ == "__main__":
    assert len(sys.argv) == 4 and sys.argv[1] == "--child" and sys.argv[2] in CASES
    try:
        result = _child(sys.argv[2], sys.argv[3])
    except BaseException as error:
        result = {
            "status": "FAILED",
            "type": type(error).__name__,
            "message": str(error),
        }
    print(json.dumps(result), flush=True)
    os._exit(0 if result["status"] == "PASS" else 1)
