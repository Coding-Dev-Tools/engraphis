"""Executable local user journeys for the Engraphis v2 surfaces.

This module is deliberately a small functional evidence runner.  It exercises
the public service/MCP boundaries against disposable local stores and emits a
content-free envelope so a run can be retained without publishing memory text,
paths, database identifiers, or exception messages.

The timings are diagnostic wall-clock observations for one local run.  They are
not throughput, latency SLO, scale, or cross-product benchmark claims.

Run the seven journeys with::

    python -m eval.user_journeys

Select one or more journeys with ``--journey``.  The runner never contacts a
model provider or a remote service.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import numpy as np

from engraphis.core.documents import DocumentScan, parse_document
from engraphis.core.interfaces import MemoryType, Scope, SearchFilter
from engraphis.core.vector_repair import index_repair_identity
from engraphis.document_import import DocumentImporter
from engraphis.factory import create_memory_engine
from engraphis.service import MemoryService


SCHEMA = "engraphis.user-journeys.v1"
RUNNER_VERSION = "1"
EVIDENCE_KIND = "runtime_journey"
AVAILABLE_JOURNEYS = (
    "mixed_document_import",
    "mcp_context_budget",
    "session_handoff",
    "code_memory_bridge",
    "concurrent_corrections",
    "index_repair",
    "erase_restart",
)

_SAFE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,80}$")
_SAFE_ERROR = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class _Observation:
    """Internal checks and aggregate counts returned by one journey."""

    checks: Mapping[str, bool]
    counts: Mapping[str, int]


@dataclass(frozen=True)
class JourneyOutcome:
    """Public-safe result for one executable journey.

    This class intentionally contains no memory IDs, text, filesystem paths,
    database paths, or exception messages.  ``as_public_dict`` is the stable
    serialization contract consumed by the campaign runner.
    """

    journey_id: str
    status: str
    checks: Mapping[str, bool]
    checks_passed: int
    checks_failed: int
    counts: Mapping[str, int]
    duration_ms: float
    error_type: Optional[str] = None

    def as_public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "journey_id": self.journey_id,
            "evidence_kind": EVIDENCE_KIND,
            "status": self.status,
            "checks": dict(self.checks),
            "checks_passed": int(self.checks_passed),
            "checks_failed": int(self.checks_failed),
            "counts": dict(self.counts),
            "duration_ms": float(self.duration_ms),
            "error_type": self.error_type,
        }
        return result


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def verify_envelope(envelope: Mapping[str, Any]) -> bool:
    """Verify the two content-free checksums emitted by :func:`run_journeys`."""

    if not isinstance(envelope, Mapping):
        return False
    if set(envelope) != {
        "schema", "payload", "payload_sha256", "envelope_sha256",
    }:
        return False
    if envelope.get("schema") != SCHEMA:
        return False
    payload = envelope.get("payload")
    payload_digest = envelope.get("payload_sha256")
    envelope_digest = envelope.get("envelope_sha256")
    if not isinstance(payload_digest, str) or not isinstance(envelope_digest, str):
        return False
    if _digest(payload) != payload_digest:
        return False
    unsigned = {
        "schema": envelope.get("schema"),
        "payload": payload,
        "payload_sha256": payload_digest,
    }
    return _digest(unsigned) == envelope_digest


def _safe_count(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, number)


def _safe_checks(checks: Mapping[str, Any]) -> dict[str, bool]:
    return {
        str(name): bool(value)
        for name, value in checks.items()
        if _SAFE_NAME.fullmatch(str(name))
    }


def _safe_counts(counts: Mapping[str, Any]) -> dict[str, int]:
    return {
        str(name): _safe_count(value)
        for name, value in counts.items()
        if _SAFE_NAME.fullmatch(str(name))
    }


def _safe_error_type(exc: BaseException) -> str:
    name = type(exc).__name__
    return name if _SAFE_ERROR.fullmatch(name) else "Exception"


def _run_one(journey_id: str, fn: Callable[[], _Observation]) -> JourneyOutcome:
    started = time.perf_counter()
    error_type: Optional[str] = None
    try:
        observation = fn()
        checks = _safe_checks(observation.checks)
        counts = _safe_counts(observation.counts)
    except SystemExit as exc:  # optional MCP installs fail closed with SystemExit
        checks = {"completed": False}
        counts = {}
        error_type = _safe_error_type(exc)
    except Exception as exc:  # noqa: BLE001 - public result records only the type
        checks = {"completed": False}
        counts = {}
        error_type = _safe_error_type(exc)
    passed = sum(1 for value in checks.values() if value)
    failed = len(checks) - passed
    status = "passed" if failed == 0 and error_type is None else "failed"
    duration_ms = round(max(0.0, (time.perf_counter() - started) * 1000.0), 3)
    return JourneyOutcome(
        journey_id=journey_id,
        status=status,
        checks=checks,
        checks_passed=passed,
        checks_failed=failed,
        counts=counts,
        duration_ms=duration_ms,
        error_type=error_type,
    )


def _local_service(db_path: str = ":memory:") -> MemoryService:
    """Create the dependency-light service used by every local journey."""

    return MemoryService.create(
        db_path,
        embed_dim=64,
        extractor="none",
        graph_extractor="none",
        retention_supervisor="none",
    )


def _scan_documents(*files: tuple[str, bytes]) -> DocumentScan:
    scan = DocumentScan(root_path="", source_id="d" * 64)
    scan.documents.extend(parse_document(raw, path) for path, raw in files)
    return scan


def _journey_mixed_document_import() -> _Observation:
    service = _local_service()
    try:
        workspace_id = service.store.get_or_create_workspace("journey-documents")
        scan = _scan_documents(
            ("notes/readme.md", b"# Read me\nSee [plan](../plan.txt).\n"),
            ("plan.txt", b"Ship the universal importer.\n"),
            ("data/info.json", b'{"title":"Facts","enabled":true}'),
        )
        importer = DocumentImporter(service)
        preview = importer.preview(
            scan,
            workspace_id=workspace_id,
            repo_id=None,
            session_id=None,
            scope=Scope.WORKSPACE,
            memory_type=MemoryType.SEMANTIC,
            source_label="Local journey documents",
        )
        first = importer.import_scan(
            scan,
            workspace_id=workspace_id,
            repo_id=None,
            session_id=None,
            scope=Scope.WORKSPACE,
            memory_type=MemoryType.SEMANTIC,
            source_label="Local journey documents",
            confirmed=True,
        )
        repeated = importer.import_scan(
            scan,
            workspace_id=workspace_id,
            repo_id=None,
            session_id=None,
            scope=Scope.WORKSPACE,
            memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"],
            source_label="Local journey documents",
            confirmed=True,
        )
        revised = _scan_documents(
            ("notes/readme.md", b"# Read me\nSee [plan](../plan.txt).\n"),
            ("plan.txt", b"Ship the improved universal importer.\n"),
            ("data/info.json", b'{"title":"Facts","enabled":true}'),
        )
        changed = importer.import_scan(
            revised,
            workspace_id=workspace_id,
            repo_id=None,
            session_id=None,
            scope=Scope.WORKSPACE,
            memory_type=MemoryType.SEMANTIC,
            source_id=first["source_id"],
            source_label="Local journey documents",
            confirmed=True,
        )
        memories = service.store.list_memories(
            SearchFilter(workspace_id=workspace_id), include_invalid=False
        )
        all_memories = service.store.list_memories(
            SearchFilter(workspace_id=workspace_id), include_invalid=True
        )
        plan_memory = next(memory for memory in all_memories if memory.title == "plan")
        history_rows = service.store.conn.execute(
            "SELECT valid_to FROM memories WHERE subject_key=? ORDER BY valid_from",
            (plan_memory.subject_key,),
        ).fetchall()
        receipt_row = service.store.conn.execute(
            "SELECT COUNT(*) AS count FROM operation_receipts "
            "WHERE operation='document_import'"
        ).fetchone()
        receipt_count = int(receipt_row["count"] if receipt_row is not None else 0)
        formats = preview.get("summary", {}).get("formats", {})
        checks = {
            "mixed_formats_preview": preview.get("counts", {}).get("documents") == 3
            and formats == {"json": 1, "markdown": 1, "text": 1},
            "first_import_completed": first.get("state") == "completed"
            and first.get("counts", {}).get("imported") == 3,
            "repeat_import_skipped": repeated.get("counts", {}).get("skipped") == 3,
            "changed_document_temporal_update": changed.get("counts", {}).get("updated") == 1
            and len(history_rows) == 2
            and sum(row["valid_to"] is None for row in history_rows) == 1,
            "source_neutral_storage": len(memories) == 3 and receipt_count == 3,
        }
        counts = {
            "preview_documents": preview.get("counts", {}).get("documents", 0),
            "imported": first.get("counts", {}).get("imported", 0),
            "reimport_skipped": repeated.get("counts", {}).get("skipped", 0),
            "updated": changed.get("counts", {}).get("updated", 0),
            "temporal_versions": len(history_rows),
            "live_documents": len(memories),
            "import_receipts": receipt_count,
        }
        return _Observation(checks=checks, counts=counts)
    finally:
        service.close()


@contextmanager
def _bound_mcp_service(service: MemoryService, mcp_server: Any) -> Iterator[Any]:
    """Bind both MCP wrappers to one disposable service for a direct call."""

    previous = mcp_server._service
    mcp_server.set_service(service)
    try:
        yield mcp_server
    finally:
        if previous is None:
            mcp_server._service = None
        else:
            mcp_server.set_service(previous)


def _load_mcp_server() -> Optional[Any]:
    """Probe the optional MCP dependency without masking runtime failures."""

    try:
        import engraphis.mcp_server as mcp_server
    except (ImportError, SystemExit):
        return None
    return mcp_server


def _tool_count(server: Any) -> int:
    loop = asyncio.new_event_loop()
    try:
        return len(loop.run_until_complete(server.list_tools()))
    finally:
        loop.close()


def _core_context_budget_fallback(service: MemoryService) -> _Observation:
    """Exercise the dependency-light budget contract when MCP is not installed.

    The MCP extra is intentionally unavailable in the Python 3.9 core-floor job.
    Keep that job meaningful without pretending that the transport/tool registry was
    tested: the fallback records the optional boundary explicitly and exercises the
    same service recall and hard context-budget path that the wrappers delegate to.
    """

    service.remember(
        "Release deployment requires a signed tag.",
        workspace="journey-mcp",
        repo="agent-repo",
    )
    service.remember(
        "Release verification requires a successful backup.",
        workspace="journey-mcp",
        repo="agent-repo",
    )
    result = service.recall(
        "release signed tag",
        workspace="journey-mcp",
        repo="agent-repo",
        k=5,
        token_budget=12,
        response_mode="full",
    )
    usage = result.get("usage", {})
    serialized = _canonical(result)
    sources = result.get("packed_sources", [])
    checks = {
        "optional_mcp_dependency_is_explicitly_gated": True,
        "core_service_serializes_as_json": isinstance(result, dict)
        and "\n" not in serialized,
        "core_budget_is_observed": usage.get("budget_tokens") == 12
        and 0 <= usage.get("context_tokens", -1) <= 12,
        "core_context_sources_are_reported": isinstance(sources, list)
        and len(sources) > 0,
    }
    counts = {
        "mcp_tools": 0,
        "core_sources": len(sources) if isinstance(sources, list) else 0,
        "core_context_tokens": usage.get("context_tokens", 0),
    }
    return _Observation(checks=checks, counts=counts)


def _journey_mcp_context_budget() -> _Observation:
    service = _local_service()
    try:
        mcp_server = _load_mcp_server()
        if mcp_server is None:
            # The MCP extra is an optional Python 3.10+ dependency.  Its absence
            # in the NumPy-only Python 3.9 core job is explicit; once imported,
            # wrapper and registry failures must remain visible to the journey.
            return _core_context_budget_fallback(service)
        with _bound_mcp_service(service, mcp_server):
            classic_remember = mcp_server.engraphis_remember(
                "Release deployment requires a signed tag.",
                workspace="journey-mcp",
                repo="agent-repo",
            )
            classic_write = json.loads(classic_remember)
            mcp_server.engraphis_remember(
                "Release verification requires a successful backup.",
                workspace="journey-mcp",
                repo="agent-repo",
            )
            classic_text = mcp_server.engraphis_recall_context(
                "release signed tag",
                workspace="journey-mcp",
                repo="agent-repo",
                k=5,
                token_budget=12,
                format="full",
            )
            smart_text = mcp_server.smart_recall_context(
                "release signed tag",
                workspace="journey-mcp",
                repo="agent-repo",
                k=5,
                token_budget=12,
                format="full",
            )
            classic = json.loads(classic_text)
            smart = json.loads(smart_text)
            required = {"context", "sources", "usage"}
            classic_usage = classic.get("usage", {})
            smart_usage = smart.get("usage", {})
            checks = {
                "classic_serializes_as_json": isinstance(classic_remember, str)
                and isinstance(classic_write, dict)
                and classic_write.get("stored") is True
                and isinstance(classic, dict)
                and "\n" not in classic_text,
                "classic_budget_is_observed": classic_usage.get("budget_tokens") == 12
                and 0 <= classic_usage.get("context_tokens", -1) <= 12,
                "smart_serializes_as_json": isinstance(smart, dict)
                and "\n" not in smart_text,
                "smart_budget_is_observed": smart_usage.get("budget_tokens") == 12
                and 0 <= smart_usage.get("context_tokens", -1) <= 12,
                "classic_smart_contract_parity": required.issubset(classic)
                and required.issubset(smart)
                and classic.get("count") == smart.get("count"),
                "both_tool_surfaces_discoverable": _tool_count(mcp_server.classic_mcp) > 0
                and _tool_count(mcp_server.smart_mcp) > 0,
            }
            counts = {
                "classic_tools": _tool_count(mcp_server.classic_mcp),
                "smart_tools": _tool_count(mcp_server.smart_mcp),
                "classic_sources": len(classic.get("sources", [])),
                "smart_sources": len(smart.get("sources", [])),
                "classic_context_tokens": classic_usage.get("context_tokens", 0),
                "smart_context_tokens": smart_usage.get("context_tokens", 0),
            }
            return _Observation(checks=checks, counts=counts)
    finally:
        service.close()


def _journey_session_handoff() -> _Observation:
    service = _local_service()
    try:
        service.remember(
            "Release verification uses a signed tag.",
            workspace="journey-sessions",
            scope="workspace",
        )
        first = service.start_session(
            "journey-sessions",
            repo="agent-repo",
            agent="codex-journey",
            goal="verify release handoff",
        )
        reused = service.start_session(
            "journey-sessions",
            repo="agent-repo",
            agent="codex-journey",
            goal="verify release handoff",
        )
        service.remember(
            "The session checked the release manifest.",
            workspace="journey-sessions",
            repo="agent-repo",
            session_id=first["session_id"],
            scope="session",
        )
        service.end_session(
            first["session_id"],
            summary="Release handoff recorded.",
            outcome="ready",
            open_threads=["review release notes"],
        )
        next_session = service.start_session(
            "journey-sessions",
            repo="agent-repo",
            agent="codex-journey",
            goal="verify release handoff",
        )
        recalled = service.recall(
            "release verification",
            workspace="journey-sessions",
            repo="agent-repo",
            session_id=next_session["session_id"],
            k=5,
            token_budget=64,
        )
        bootstrap = next_session.get("bootstrap", {})
        checks = {
            "exact_start_reuses_active_session": first["session_id"] == reused["session_id"]
            and reused.get("reused") is True,
            "ended_start_creates_new_session": next_session["session_id"] != first["session_id"]
            and next_session.get("reused") is False,
            "handoff_bootstrap_is_restored": bootstrap.get("outcome") == "ready"
            and bootstrap.get("open_threads") == ["review release notes"],
            "next_session_recalls_workspace_context": recalled.get("count", 0) >= 1,
        }
        counts = {
            "sessions_started": 2,
            "reused_starts": int(bool(reused.get("reused"))),
            "bootstrap_open_threads": len(bootstrap.get("open_threads", [])),
            "recalled_memories": recalled.get("count", 0),
        }
        return _Observation(checks=checks, counts=counts)
    finally:
        service.close()


def _journey_code_memory_bridge() -> _Observation:
    with tempfile.TemporaryDirectory(prefix="engraphis-journey-code-") as root:
        source = Path(root) / "deploy.py"
        source.write_text(
            "def deploy_release(config):\n"
            "    return config\n\n"
            "def helper():\n"
            "    return deploy_release({})\n",
            encoding="utf-8",
        )
        service = _local_service()
        try:
            indexed = service.index_repo(
                workspace="journey-code",
                repo="agent-repo",
                root_path=root,
            )
            remembered = service.remember(
                "deploy_release requires a signed tag and successful backup.",
                workspace="journey-code",
                repo="agent-repo",
                mtype="procedural",
            )
            searched = service.search_code(
                "deploy_release",
                workspace="journey-code",
                repo="agent-repo",
                limit=20,
            )
            recalled = service.recall(
                "What calls deploy_release?",
                workspace="journey-code",
                repo="agent-repo",
                k=5,
                retrieval_profile="code",
                diagnostics=True,
            )
            symbols = searched.get("symbols", [])
            links = [
                memory
                for symbol in symbols
                for memory in symbol.get("linked_memories", [])
            ]
            bridge_hits = sum(1 for memory in links if memory.get("id") == remembered["id"])
            trace = recalled.get("retrieval_trace", [])
            code_hits = sum(1 for item in trace if "code" in item.get("arms", []))
            checks = {
                "repository_symbols_indexed": indexed.get("files_indexed", 0) >= 1
                and indexed.get("symbols", 0) >= 1,
                "code_search_returns_symbol": len(symbols) >= 1,
                "symbol_memory_bridge_exists": bridge_hits >= 1,
                "code_profile_recalls_memory": remembered["id"]
                in {item.get("id") for item in recalled.get("memories", [])},
                "code_arm_is_observed": code_hits >= 1,
            }
            counts = {
                "files_indexed": indexed.get("files_indexed", 0),
                "symbols": indexed.get("symbols", 0),
                "edges": indexed.get("edges", 0),
                "linked_memories": len(links),
                "bridge_hits": bridge_hits,
                "code_arm_hits": code_hits,
            }
            return _Observation(checks=checks, counts=counts)
        finally:
            service.close()


def _journey_concurrent_corrections() -> _Observation:
    with tempfile.TemporaryDirectory(prefix="engraphis-journey-corrections-") as root:
        db_path = str(Path(root) / "corrections.db")
        initial = create_memory_engine(db_path, auto_evolve=False)
        workspace_id = initial.store.get_or_create_workspace("journey-corrections")
        repo_id = initial.store.get_or_create_repo(workspace_id, "agent-repo")
        initial_result = initial.remember_with_resolution(
            "The cache TTL is 30 seconds.",
            workspace_id=workspace_id,
            repo_id=repo_id,
            scope=Scope.REPO,
            subject_key="cache",
            claim_kind="ttl",
        )
        initial.close()
        engines = [create_memory_engine(db_path, auto_evolve=False) for _ in range(2)]
        barrier = threading.Barrier(2, timeout=20)
        try:
            for engine in engines:
                original_embed = engine.embedder.embed

                def synchronized_embed(
                    texts: Sequence[str],
                    *,
                    kind: str = "text",
                    original: Callable[..., Any] = original_embed,
                ) -> Any:
                    vectors = original(texts, kind=kind)
                    barrier.wait()
                    return vectors

                engine.embedder.embed = synchronized_embed

            def correct(engine: Any, seconds: int) -> dict[str, Any]:
                return engine.remember_with_resolution(
                    f"The cache TTL is {seconds} seconds.",
                    workspace_id=workspace_id,
                    repo_id=repo_id,
                    scope=Scope.REPO,
                    subject_key="cache",
                    claim_kind="ttl",
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(correct, engines, (60, 90)))
            history = engines[0].store.list_memories(
                SearchFilter(workspace_id=workspace_id, repo_id=repo_id),
                include_invalid=True,
            )
            live = engines[0].store.list_memories(
                SearchFilter(workspace_id=workspace_id, repo_id=repo_id),
                include_invalid=False,
            )
            statuses = [result.get("op") for result in results]
            checks = {
                "both_corrections_commit": len(results) == 2
                and all(status == "invalidate" for status in statuses),
                "single_current_claim_remains": len(live) == 1,
                "correction_history_is_preserved": len(history) == 3
                and sum(record.valid_to is None for record in history) == 1,
                "initial_claim_is_superseded": initial_result["id"]
                not in {record.id for record in live},
            }
            counts = {
                "attempts": 2,
                "committed_corrections": len(results),
                "conflicts": 0,
                "history_versions": len(history),
                "live_claims": len(live),
            }
            return _Observation(checks=checks, counts=counts)
        finally:
            for engine in engines:
                engine.close()


class _JourneyExternalIndex:
    """Tiny local external-index double for the real durable repair protocol."""

    index_identity = "engraphis-user-journeys-index-v1"

    def __init__(self) -> None:
        self.rows: dict[str, np.ndarray] = {}
        self.fail = False

    def search(self, vector: np.ndarray, k: int, *, filter: Any = None) -> list[tuple[str, float]]:
        query = np.asarray(vector, dtype=np.float32)
        norm = max(float(np.linalg.norm(query)), 1e-12)
        query = query / norm
        return sorted(
            ((memory_id, float(value @ query)) for memory_id, value in self.rows.items()),
            key=lambda item: (-item[1], item[0]),
        )[:k]

    def upsert(
        self,
        ids: Sequence[str],
        vectors: Sequence[np.ndarray],
        meta: Any = None,
        *,
        commit: bool = True,
    ) -> None:
        if self.fail:
            raise RuntimeError("journey index unavailable")
        for memory_id, vector in zip(ids, vectors):
            value = np.asarray(vector, dtype=np.float32)
            self.rows[str(memory_id)] = value / max(float(np.linalg.norm(value)), 1e-12)

    def delete(self, ids: Sequence[str], *, commit: bool = True) -> None:
        if self.fail:
            raise RuntimeError("journey index unavailable")
        for memory_id in ids:
            self.rows.pop(str(memory_id), None)


def _journey_index_repair() -> _Observation:
    engine = create_memory_engine(":memory:", auto_evolve=False)
    index = _JourneyExternalIndex()
    engine.index = index
    engine.recall_engine.index = index
    try:
        workspace_id = engine.store.get_or_create_workspace("journey-repair")
        first_id = engine.remember(
            "The durable repair fixture is present.", workspace_id=workspace_id
        )
        # The production engine intentionally logs the memory id when an external
        # provider rejects a publication.  Silence that expected outage while this
        # content-free runner creates its durable repair debt.
        engine_logger = logging.getLogger("engraphis.core.vector_repair")
        previous_disabled = engine_logger.disabled
        engine_logger.disabled = True
        try:
            index.fail = True
            second_id = engine.remember(
                "The durable repair fixture needs replay.", workspace_id=workspace_id
            )
        finally:
            engine_logger.disabled = previous_disabled
        target = index_repair_identity(index, engine.store)
        pending_before = engine.store.vector_index_pending(target) if target else None
        index.fail = False
        repaired = engine.repair_vector_index()
        pending_after = engine.store.vector_index_pending(target) if target else None
        checks = {
            "failed_publication_is_queued": pending_before == 1,
            "repair_replays_canonical_vector": repaired.get("repaired") == 1
            and repaired.get("pending") == 0,
            "repaired_memory_is_searchable": second_id in index.rows,
            "healthy_vector_remains_present": first_id in index.rows,
            "queue_is_acknowledged": pending_after == 0,
        }
        counts = {
            "queued": pending_before or 0,
            "attempted": repaired.get("attempted", 0),
            "repaired": repaired.get("repaired", 0),
            "pending_after": pending_after or 0,
            "indexed_rows": len(index.rows),
        }
        return _Observation(checks=checks, counts=counts)
    finally:
        engine.close()


def _journey_erase_restart() -> _Observation:
    with tempfile.TemporaryDirectory(prefix="engraphis-journey-erase-") as root:
        db_path = str(Path(root) / "erase.db")
        service = _local_service(db_path)
        stored = service.remember(
            "A disposable private journey datum.",
            workspace="journey-erase",
            repo="agent-repo",
        )
        memory_id = stored["id"]
        service.close()
        reopened = _local_service(db_path)
        try:
            before = reopened.store.get_memory(memory_id) is not None
            erased = reopened.secure_erase(
                memory_id,
                workspace="journey-erase",
                repo="agent-repo",
                confirmed=True,
            )
            after_erase = reopened.store.get_memory(memory_id) is None
            tombstone_row = reopened.store.conn.execute(
                "SELECT COUNT(*) AS count FROM memory_tombstones"
            ).fetchone()
            tombstones = int(tombstone_row["count"] if tombstone_row is not None else 0)
            reopened.close()
            restarted = _local_service(db_path)
            try:
                after_restart = restarted.store.get_memory(memory_id) is None
                remaining = restarted.store.count_memories()
            finally:
                restarted.close()
            checks = {
                "datum_exists_after_first_restart": before,
                "explicit_erase_is_confirmed": erased.get("status") == "securely_erased",
                "datum_is_removed_before_close": after_erase,
                "tombstone_is_recorded": tombstones >= 1,
                "datum_stays_erased_after_restart": after_restart and remaining == 0,
            }
            counts = {
                "erased": int(erased.get("status") == "securely_erased"),
                "tombstones": tombstones,
                "post_restart_memories": remaining,
            }
            return _Observation(checks=checks, counts=counts)
        finally:
            # ``close`` is idempotent, so this also covers a failed erase path.
            reopened.close()


_JOURNEY_FUNCTIONS: dict[str, Callable[[], _Observation]] = {
    "mixed_document_import": _journey_mixed_document_import,
    "mcp_context_budget": _journey_mcp_context_budget,
    "session_handoff": _journey_session_handoff,
    "code_memory_bridge": _journey_code_memory_bridge,
    "concurrent_corrections": _journey_concurrent_corrections,
    "index_repair": _journey_index_repair,
    "erase_restart": _journey_erase_restart,
}


def run_journey(journey_id: str) -> dict[str, Any]:
    """Execute one named journey and return its public-safe outcome."""

    if journey_id not in _JOURNEY_FUNCTIONS:
        raise ValueError(f"unknown journey: {journey_id}")
    return _run_one(journey_id, _JOURNEY_FUNCTIONS[journey_id]).as_public_dict()


def run_journeys(journeys: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Execute selected journeys and return a checksummed public-safe envelope."""

    selected = list(AVAILABLE_JOURNEYS if journeys is None else journeys)
    if not selected:
        raise ValueError("at least one journey is required")
    unknown = [journey for journey in selected if journey not in _JOURNEY_FUNCTIONS]
    if unknown:
        raise ValueError("unknown journey: " + ", ".join(unknown))
    if len(set(selected)) != len(selected):
        raise ValueError("journeys must be unique")
    outcomes = [run_journey(journey) for journey in selected]
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "runner_version": RUNNER_VERSION,
        "evidence_kind": EVIDENCE_KIND,
        "execution": "offline_local_disposable_stores",
        "claim_boundary": "functional regression evidence; timings are diagnostic only",
        "journey_count": len(outcomes),
        "passed": sum(item["status"] == "passed" for item in outcomes),
        "failed": sum(item["status"] != "passed" for item in outcomes),
        "journeys": outcomes,
    }
    payload_digest = _digest(payload)
    unsigned = {
        "schema": SCHEMA,
        "payload": payload,
        "payload_sha256": payload_digest,
    }
    return {
        **unsigned,
        "envelope_sha256": _digest(unsigned),
    }


def run(journeys: Optional[Sequence[str]] = None) -> dict[str, Any]:
    """Compatibility alias for evaluation callers that use the conventional ``run``."""

    return run_journeys(journeys)


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--journey",
        action="append",
        choices=AVAILABLE_JOURNEYS,
        help="run one named journey; repeat to select multiple (default: all)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    envelope = run_journeys(args.journey)
    print(_canonical(envelope))
    return 0 if envelope["payload"]["failed"] == 0 else 1


if __name__ == "__main__":  # pragma: no cover - exercised by the CLI smoke
    raise SystemExit(main())


__all__ = [
    "AVAILABLE_JOURNEYS",
    "EVIDENCE_KIND",
    "JourneyOutcome",
    "SCHEMA",
    "main",
    "run",
    "run_journey",
    "run_journeys",
    "verify_envelope",
]
