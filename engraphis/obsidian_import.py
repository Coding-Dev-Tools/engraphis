"""Production Obsidian import orchestration for the v2 memory engine.

The dependency-free parser lives in :mod:`engraphis.core.obsidian`.  This outer
module owns persistence and deliberately receives an already-composed
``MemoryService`` so no concrete backend crosses into ``core``.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re
import time
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence
import unicodedata
from urllib.parse import unquote, urlsplit

from engraphis.core.ids import new_id
from engraphis.core.interfaces import GraphLayer, MemoryType, Scope
from engraphis.core.obsidian import (
    IMPORTER_VERSION,
    MAX_NOTE_BYTES,
    MAX_VAULT_BYTES,
    MAX_VAULT_FILES,
    ObsidianFileIssue,
    ObsidianVaultScan,
    normalize_obsidian_path,
    parse_obsidian_note,
)


_SAFE_ERROR = "note import failed"
_CONFLICT_POLICIES = {"error", "replace", "new"}
_ACTIVE_ITEM_STATES = {"imported", "unchanged", "renamed", "skipped"}
_SENSITIVE_NAMES = {
    ".env", "credentials", "credentials.json", "id_dsa", "id_rsa",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "secret", "secret.json",
    "secrets", "secrets.json", "token", "tokens",
}


class _ImportLink(Protocol):
    """The source-neutral link shape consumed by the import planner."""

    @property
    def target(self) -> str: ...

    @property
    def display_text(self) -> Optional[str]: ...

    @property
    def heading(self) -> Optional[str]: ...

    @property
    def block_id(self) -> Optional[str]: ...

    @property
    def embedded(self) -> bool: ...


class _ImportAttachment(Protocol):
    """The source-neutral attachment shape consumed by the import planner."""

    @property
    def path(self) -> str: ...


class _ImportNote(Protocol):
    """Readable source record accepted by the temporal import planner.

    Both ``ObsidianNote`` and the universal ``DocumentRecord`` deliberately
    implement this narrow, read-only shape.  Keeping it structural prevents the
    document adapter from inheriting an Obsidian-only type contract while keeping
    the runtime planner entirely source-neutral.
    """

    @property
    def relative_path(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def body(self) -> str: ...

    @property
    def raw_sha256(self) -> str: ...

    @property
    def canonical_sha256(self) -> str: ...

    @property
    def source_size(self) -> int: ...

    @property
    def source_mtime_ns(self) -> Optional[int]: ...

    @property
    def title_source(self) -> str: ...

    @property
    def aliases(self) -> Sequence[str]: ...

    @property
    def tags(self) -> Sequence[str]: ...

    @property
    def dates(self) -> dict[str, str]: ...

    @property
    def headings(self) -> Sequence[str]: ...

    @property
    def links(self) -> Sequence[_ImportLink]: ...

    @property
    def attachments(self) -> Sequence[_ImportAttachment]: ...

    @property
    def warnings(self) -> Sequence[str]: ...


class _ImportIssue(Protocol):
    @property
    def relative_path(self) -> str: ...

    @property
    def reason(self) -> str: ...


class _ImportScan(Protocol):
    """Minimal source collection shape needed for plan/report execution."""

    @property
    def vault_id(self) -> str: ...

    @property
    def notes(self) -> Sequence[_ImportNote]: ...

    @property
    def rejected(self) -> Sequence[_ImportIssue]: ...

    @property
    def skipped(self) -> Sequence[_ImportIssue]: ...

    @property
    def complete(self) -> bool: ...


class ObsidianImportCancelled(Exception):
    """Raised at a note boundary after a caller requests cancellation."""


@dataclass
class _Plan:
    note: _ImportNote
    action: str
    item: Optional[dict] = None
    reason: str = ""


def scan_obsidian_upload(
    files: Iterable[tuple[str, bytes]], *, vault_label: str,
) -> ObsidianVaultScan:
    """Parse browser-selected Markdown bytes without persisting an upload copy."""
    label = unicodedata.normalize("NFC", str(vault_label or "").strip()[:200])
    if not label:
        raise ValueError("vault_label is required for a browser source")
    root_digest = hashlib.sha256(
        ("obsidian-browser\0" + label.casefold()).encode("utf-8", "surrogatepass")
    ).hexdigest()
    scan = ObsidianVaultScan(vault_path="", vault_id=root_digest)
    total = 0
    seen: set[str] = set()
    for index, (raw_path, raw) in enumerate(files):
        if index >= MAX_VAULT_FILES:
            scan.rejected.append(ObsidianFileIssue(
                "(vault)", "vault exceeds Markdown file safety limit",
            ))
            scan.complete = False
            break
        try:
            relative_path = normalize_obsidian_path(raw_path)
        except ValueError:
            scan.rejected.append(ObsidianFileIssue("(invalid path)", "invalid source path"))
            continue
        parts = PurePosixPath(relative_path).parts
        if any(part.startswith(".") for part in parts):
            scan.skipped.append(ObsidianFileIssue(relative_path, "hidden/configuration path skipped"))
            continue
        portable_path = relative_path.casefold()
        if portable_path in seen:
            scan.rejected.append(ObsidianFileIssue(relative_path, "duplicate upload path"))
            continue
        seen.add(portable_path)
        if not relative_path.casefold().endswith(".md"):
            scan.skipped.append(ObsidianFileIssue(relative_path, "non-Markdown file skipped"))
            continue
        name = parts[-1].casefold()
        if _sensitive_name(name):
            scan.rejected.append(ObsidianFileIssue(relative_path, "sensitive filename"))
            continue
        if not isinstance(raw, bytes):
            scan.rejected.append(ObsidianFileIssue(relative_path, "invalid upload"))
            continue
        if len(raw) > MAX_NOTE_BYTES:
            scan.rejected.append(ObsidianFileIssue(relative_path, "note exceeds byte safety limit"))
            continue
        total += len(raw)
        if total > MAX_VAULT_BYTES:
            scan.rejected.append(ObsidianFileIssue(
                relative_path, "vault exceeds total byte safety limit",
            ))
            scan.complete = False
            break
        try:
            scan.notes.append(parse_obsidian_note(raw, relative_path))
        except ValueError as exc:
            scan.rejected.append(ObsidianFileIssue(relative_path, _safe_parse_reason(exc)))
    return scan


def _sensitive_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _SENSITIVE_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith((".key", ".p12", ".pem", ".pfx"))
        or bool(re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", lowered))
    )


def _safe_parse_reason(exc: BaseException) -> str:
    text = str(exc)
    allowed = (
        "secret", "character safety limit", "byte safety limit", "invalid source path",
    )
    return next((f"source rejected: {label}" for label in allowed if label in text), "source rejected")


def stable_source_key(vault_id: str, relative_path: str, *, branch: str = "") -> str:
    material = f"{vault_id}\0{normalize_obsidian_path(relative_path)}\0{branch}"
    return hashlib.sha256(material.encode("utf-8", "surrogatepass")).hexdigest()


class ObsidianImporter:
    """Plan and execute repeatable imports through one injected v2 service."""

    SOURCE_KIND = "obsidian"
    JOB_KIND = "obsidian_import"
    RECEIPT_OPERATION = "obsidian_import"
    CLAIM_KIND = "obsidian_note"
    SUBJECT_PREFIX = "obsidian"
    METADATA_KEY = "obsidian"
    DEFAULT_LABEL = "Obsidian vault"
    IMPORTER_VERSION = IMPORTER_VERSION
    COUNT_KEY = "markdown"
    LINK_REASON = "obsidian_wikilink"
    LINK_IMPORTED_ATTACHMENTS = False

    def __init__(self, service: Any = None) -> None:
        self.service = service
        # ``Any`` is deliberate: preview-only CLI construction has no Store at all,
        # while live construction receives the service's concrete engine/Store pair.
        self.engine: Any = service.engine if service is not None else None
        self.store: Any = service.store if service is not None else None

    def preview(
        self, scan: _ImportScan, *, workspace_id: Optional[str],
        repo_id: Optional[str], session_id: Optional[str], scope: Scope,
        memory_type: MemoryType, vault_id: Optional[str] = None,
        vault_label: str = "", on_conflict: str = "error",
        manifest: Optional[dict] = None, strict_root: bool = True,
        attachment_manifest: Optional[list[dict]] = None,
    ) -> dict:
        policy = self._policy(on_conflict)
        vault, items = self._preview_manifest(
            scan, workspace_id=workspace_id, repo_id=repo_id,
            session_id=session_id, vault_id=vault_id, manifest=manifest,
            scope=scope, memory_type=memory_type, strict_root=strict_root,
        )
        identity = str((vault or {}).get("id") or f"preview:{scan.vault_id}")
        plans, missing = self._plan(
            scan, identity, items, inspect_memories=manifest is None,
        )
        return self._report(
            plans, missing, scan, state="preview", vault_id=(vault or {}).get("id"),
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scope=scope, memory_type=memory_type, policy=policy,
            vault_label=vault_label, attachment_manifest=attachment_manifest,
        )

    def import_scan(
        self, scan: _ImportScan, *, workspace_id: str,
        repo_id: Optional[str], session_id: Optional[str], scope: Scope,
        memory_type: MemoryType, vault_id: Optional[str] = None,
        vault_label: str = "", on_conflict: str = "error",
        confirmed: bool = False, actor: str = "local_cli_operator",
        strict_root: bool = True, attachment_manifest: Optional[list[dict]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        progress: Optional[Callable[[dict], None]] = None,
        prepared: Optional[dict] = None,
    ) -> dict:
        if confirmed is not True:
            raise ValueError("trusted-local confirmation is required")
        policy = self._policy(on_conflict)
        prepared = prepared or self.prepare_import(
            scan, workspace_id=workspace_id, repo_id=repo_id,
            session_id=session_id, scope=scope, memory_type=memory_type,
            vault_id=vault_id, vault_label=vault_label, on_conflict=policy,
            confirmed=True, strict_root=strict_root,
        )
        vault_id = str(prepared["vault_id"])
        run_started = time.time()
        job_id = str(prepared["job_id"])
        import_id = str(prepared["import_id"])
        items = self.store.list_source_import_items(vault_id=vault_id)
        plans, missing = self._plan(scan, vault_id, items, inspect_memories=True)
        for plan in plans:
            self.store.record_source_import_job_item(
                job_id=job_id, source_id=(plan.item or {}).get("id"),
                relative_path=plan.note.relative_path,
                planned_action=plan.action, result_state="pending",
                warning_count=len(plan.note.warnings),
                source_format=str(getattr(plan.note, "format", "markdown"))[:64],
            )
        for issue in scan.rejected:
            self.store.record_source_import_job_item(
                job_id=job_id, relative_path=issue.relative_path,
                planned_action="rejected", result_state="rejected",
                error_code="source_rejected",
            )
        for issue in scan.skipped:
            self.store.record_source_import_job_item(
                job_id=job_id, relative_path=issue.relative_path,
                planned_action="skipped", result_state="skipped",
            )
        for item in missing:
            self.store.record_source_import_job_item(
                job_id=job_id, source_id=item.get("id"),
                relative_path=str(item.get("relative_path") or "(missing)"),
                planned_action="missing", result_state="pending",
            )
        outcomes: list[dict] = []
        finalized_missing: list[dict] = []
        pending_missing = list(missing)
        unreadable_directories = self._unreadable_directories(scan)
        can_finalize_missing = scan.complete and not unreadable_directories
        terminal_state = "completed"
        try:
            for index, plan in enumerate(plans, 1):
                self._check_cancel(job_id, cancel_check)
                outcome = self._apply_plan(
                    plan, vault_id=vault_id, import_id=import_id,
                    workspace_id=workspace_id, repo_id=repo_id,
                    session_id=session_id, scope=scope, memory_type=memory_type,
                    policy=policy, actor=actor,
                )
                outcomes.append(outcome)
                self._update_job_progress(job_id, index, outcomes)
                if progress is not None:
                    progress(dict(outcome))
            self._check_cancel(job_id, cancel_check)
            if can_finalize_missing:
                self.store.mark_source_import_items_missing(
                    vault_id=vault_id, seen_before=run_started,
                    preserve_paths=self._rejected_paths(scan),
                )
                for item in missing:
                    self.store.record_source_import_job_item(
                        job_id=job_id, source_id=item.get("id"),
                        relative_path=str(item.get("relative_path") or "(missing)"),
                        planned_action="missing", result_state="missing",
                    )
                finalized_missing = missing
                pending_missing = []
            # Link reconciliation happens after every note has a durable current id.
            link_warnings = self._reconcile_links(
                scan, vault_id=vault_id, job_id=job_id, cancel_check=cancel_check,
            )
            outcomes.extend(link_warnings)
            if scan.rejected or not scan.complete or unreadable_directories or any(
                row["status"] in {"error", "conflict", "rejected"} for row in outcomes
            ):
                terminal_state = "partial"
        except (KeyboardInterrupt, ObsidianImportCancelled):
            terminal_state = "cancelled"
        except Exception:
            terminal_state = "failed"
        report = self._final_report(
            plans, outcomes, finalized_missing, scan, state=terminal_state,
            pending_missing=pending_missing,
            vault_id=vault_id, job_id=job_id, import_id=import_id,
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scope=scope, memory_type=memory_type, policy=policy,
            vault_label=vault_label, attachment_manifest=attachment_manifest,
        )
        if terminal_state == "completed" and report["counts"].get("conflict", 0):
            terminal_state = "partial"
            report["state"] = terminal_state
        self._finish_job(job_id, terminal_state, report)
        self._record_receipt(
            report, workspace_id=workspace_id, repo_id=repo_id, actor=actor,
        )
        return report

    def prepare_import(
        self, scan: _ImportScan, *, workspace_id: str,
        repo_id: Optional[str], session_id: Optional[str], scope: Scope,
        memory_type: MemoryType, vault_id: Optional[str] = None,
        vault_label: str = "", on_conflict: str = "error",
        confirmed: bool = False, strict_root: bool = True,
    ) -> dict:
        """Persist only the run header so a dashboard worker can start asynchronously."""
        if confirmed is not True:
            raise ValueError("trusted-local confirmation is required")
        policy = self._policy(on_conflict)
        vault = self._resolve_or_register_vault(
            scan, workspace_id=workspace_id, repo_id=repo_id,
            session_id=session_id, scope=scope, memory_type=memory_type,
            vault_id=vault_id, label=vault_label, strict_root=strict_root,
        )
        selected_vault_id = str(vault["id"])
        job_id = self._create_job(
            workspace_id, repo_id, session_id=session_id,
            total=len(scan.notes) + len(scan.rejected) + len(scan.skipped),
            policy=policy, scope=scope, memory_type=memory_type,
        )
        return {
            "vault_id": selected_vault_id, "job_id": job_id,
            "import_id": job_id,
        }

    @staticmethod
    def _policy(value: str) -> str:
        policy = str(value or "error").strip().casefold()
        policy = {"report": "error", "supersede": "replace"}.get(policy, policy)
        if policy not in _CONFLICT_POLICIES:
            raise ValueError("on_conflict must be error, replace, or new")
        return policy

    def _preview_manifest(
        self, scan: _ImportScan, *, workspace_id: Optional[str],
        repo_id: Optional[str], session_id: Optional[str], vault_id: Optional[str],
        scope: Scope, memory_type: MemoryType, manifest: Optional[dict],
        strict_root: bool,
    ) -> tuple[Optional[dict], list[dict]]:
        vaults = (
            list((manifest or {}).get("vaults") or [])
            if manifest is not None else self.store.list_source_vaults(kind=self.SOURCE_KIND)
        )
        items = (
            list((manifest or {}).get("items") or [])
            if manifest is not None else []
        )
        vault: Optional[dict] = None
        if vault_id:
            vault = (
                self.store.get_source_vault(vault_id)
                if manifest is None else
                next((row for row in vaults if row.get("id") == vault_id), None)
            )
            if vault is None:
                raise ValueError("registered vault was not found")
        else:
            matches = [
                row for row in vaults
                if row.get("kind") == self.SOURCE_KIND
                and row.get("root_digest") == scan.vault_id
                and (workspace_id is None or row.get("workspace_id") == workspace_id)
                and row.get("repo_id") == repo_id
                and row.get("session_id") == session_id
            ]
            vault = matches[0] if len(matches) == 1 else None
        if vault is not None:
            self._validate_vault_target(
                vault, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, root_digest=scan.vault_id,
                scope=scope, memory_type=memory_type, strict_root=strict_root,
            )
            if manifest is None:
                items = self.store.list_source_import_items(vault_id=str(vault["id"]))
            else:
                items = [row for row in items if row.get("vault_id") == vault.get("id")]
        else:
            items = []
        return vault, items

    def _resolve_or_register_vault(
        self, scan: _ImportScan, *, workspace_id: str,
        repo_id: Optional[str], session_id: Optional[str], scope: Scope,
        memory_type: MemoryType, vault_id: Optional[str], label: str,
        strict_root: bool,
    ) -> dict:
        if vault_id:
            vault = self.store.get_source_vault(vault_id)
            if vault is None:
                raise ValueError("registered vault was not found")
            self._validate_vault_target(
                vault, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, root_digest=scan.vault_id,
                scope=scope, memory_type=memory_type, strict_root=strict_root,
            )
            return vault
        vault_id = self.store.register_source_vault(
            kind=self.SOURCE_KIND, root_digest=scan.vault_id,
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            display_name=str(label or self.DEFAULT_LABEL)[:200], scope=scope.value,
            memory_type=memory_type.value, importer_version=self.IMPORTER_VERSION,
        )
        vault = self.store.get_source_vault(vault_id)
        if vault is None:
            raise RuntimeError("registered vault identity was not persisted")
        return vault

    @classmethod
    def _validate_vault_target(
        cls, vault: dict, *, workspace_id: Optional[str], repo_id: Optional[str],
        session_id: Optional[str], root_digest: str, scope: Scope,
        memory_type: MemoryType, strict_root: bool,
    ) -> None:
        if vault.get("kind") != cls.SOURCE_KIND:
            raise ValueError("registered source uses a different import adapter")
        if workspace_id is not None and vault.get("workspace_id") != workspace_id:
            raise ValueError("registered vault belongs to another workspace")
        if vault.get("repo_id") != repo_id or vault.get("session_id") != session_id:
            raise ValueError("registered vault has a different target scope")
        if vault.get("scope") != scope.value or vault.get("memory_type") != memory_type.value:
            raise ValueError("registered vault has different import defaults")
        if strict_root and vault.get("root_digest") != root_digest:
            raise ValueError("selected path does not match the registered vault")

    def _plan(
        self, scan: _ImportScan, vault_identity: str, items: list[dict], *,
        inspect_memories: bool,
    ) -> tuple[list[_Plan], list[dict]]:
        scan_paths = {
            note.relative_path for note in scan.notes
        } | self._rejected_paths(scan)
        unreadable_directories = self._unreadable_directories(scan)
        by_path: dict[str, list[dict]] = {}
        for item in items:
            relative_path = str(item.get("relative_path") or "")
            by_path.setdefault(relative_path, []).append(item)
            if self._under_directory(relative_path, unreadable_directories):
                scan_paths.add(relative_path)
        # Exact-content rename detection stays conservative but must remain
        # linear for a full 10k-file source. Index eligible historical paths once.
        renames_by_hash: dict[str, list[dict]] = {}
        for item in items:
            relative_path = str(item.get("relative_path") or "")
            content_hash = str(item.get("content_sha256") or "")
            if (
                relative_path not in scan_paths
                and content_hash
                and item.get("state") != "conflict"
            ):
                renames_by_hash.setdefault(content_hash, []).append(item)
        used: set[str] = set()
        plans: list[_Plan] = []
        for note in sorted(scan.notes, key=lambda entry: entry.relative_path.casefold()):
            candidates = list(by_path.get(note.relative_path, []))
            exact = [
                row for row in candidates
                if row.get("content_sha256") == note.raw_sha256
                and row.get("importer_version") == self.IMPORTER_VERSION
                and row.get("state") != "conflict"
            ]
            selected = self._newest(exact) if exact else self._newest([
                row for row in candidates if row.get("state") in _ACTIVE_ITEM_STATES
            ])
            if selected is None and candidates:
                selected = self._newest(candidates)
            if selected is not None:
                used.add(str(selected.get("source_key")))
                if exact:
                    action = "skipped"
                    reason = "unchanged"
                else:
                    action = "updated"
                    reason = "source_changed"
                if inspect_memories and not self._manifest_memory_is_current(selected):
                    action, reason = "conflict", "memory_lineage_diverged"
                plans.append(_Plan(note, action, selected, reason))
                continue
            rename_candidates = [
                row for row in renames_by_hash.get(note.raw_sha256, ())
                if row.get("source_key") not in used
            ]
            if len(rename_candidates) == 1:
                selected = rename_candidates[0]
                used.add(str(selected.get("source_key")))
                action, reason = "renamed", "unique_content_path_move"
                if inspect_memories and not self._manifest_memory_is_current(selected):
                    action, reason = "conflict", "memory_lineage_diverged"
                plans.append(_Plan(note, action, selected, reason))
            elif len(rename_candidates) > 1:
                plans.append(_Plan(note, "conflict", None, "ambiguous_rename"))
            else:
                plans.append(_Plan(note, "imported", None, "new_source"))
        missing = [
            row for row in items
            if row.get("relative_path") not in scan_paths
            and row.get("source_key") not in used
            and row.get("state") not in {"missing", "conflict"}
        ]
        return plans, missing

    @staticmethod
    def _rejected_paths(scan: _ImportScan) -> set[str]:
        """Keep durable rows for files seen but rejected by the parser."""
        return {str(issue.relative_path) for issue in scan.rejected}

    @staticmethod
    def _unreadable_directories(scan: _ImportScan) -> set[str]:
        return {
            str(issue.relative_path).rstrip("/")
            for issue in scan.skipped
            if str(issue.reason) == "unreadable directory"
        }

    @staticmethod
    def _under_directory(relative_path: str, directories: set[str]) -> bool:
        return any(
            relative_path == directory or relative_path.startswith(directory + "/")
            for directory in directories
        )

    @staticmethod
    def _newest(items: list[dict]) -> Optional[dict]:
        return max(
            items,
            key=lambda row: (float(row.get("last_seen_at") or 0), str(row.get("id") or "")),
            default=None,
        )

    def _manifest_memory_is_current(self, item: dict) -> bool:
        memory_id = str(item.get("memory_id") or "")
        subject_key = str(item.get("subject_key") or "")
        if not memory_id:
            return False
        rec = self.store.get_memory(memory_id)
        if rec is None or rec.valid_to is not None or rec.expired_at is not None:
            return False
        live_id = self._live_subject_memory(subject_key)
        if live_id and live_id != memory_id:
            return False
        source_metadata = (
            rec.metadata.get(self.METADATA_KEY) if isinstance(rec.metadata, dict) else None
        )
        return (
            isinstance(source_metadata, dict)
            and source_metadata.get("raw_sha256") == item.get("content_sha256")
            and source_metadata.get("source_id") == item.get("id")
        )

    def _live_subject_memory(self, subject_key: str) -> Optional[str]:
        if not subject_key:
            return None
        row = self.store.conn.execute(
            "SELECT id FROM memories WHERE subject_key=? AND valid_to IS NULL "
            "AND expired_at IS NULL ORDER BY valid_from DESC, ingested_at DESC, id DESC LIMIT 1",
            (subject_key,),
        ).fetchone()
        return str(row["id"]) if row is not None else None

    def _apply_plan(
        self, plan: _Plan, *, vault_id: str, import_id: str,
        workspace_id: str, repo_id: Optional[str], session_id: Optional[str],
        scope: Scope, memory_type: MemoryType, policy: str, actor: str,
    ) -> dict:
        note = plan.note
        if plan.action == "skipped" and plan.item is not None:
            self.store.upsert_source_import_item(
                vault_id=vault_id, source_key=str(plan.item["source_key"]),
                source_id=str(plan.item["id"]), relative_path=note.relative_path,
                memory_id=plan.item.get("memory_id"),
                subject_key=str(plan.item.get("subject_key") or ""),
                content_sha256=note.raw_sha256, canonical_sha256=note.canonical_sha256,
                file_size=note.source_size,
                file_mtime_ns=note.source_mtime_ns,
                importer_version=self.IMPORTER_VERSION,
                state="unchanged", import_id=import_id,
            )
            self.store.record_source_import_job_item(
                job_id=import_id, source_id=str(plan.item["id"]),
                relative_path=note.relative_path, planned_action="skipped",
                result_state="skipped", warning_count=len(note.warnings),
                source_format=str(getattr(note, "format", "markdown"))[:64],
            )
            return self._outcome(note, "skipped", "unchanged")
        if plan.action == "conflict" and policy == "error":
            if plan.item is not None:
                self.store.upsert_source_import_item(
                    vault_id=vault_id, source_key=str(plan.item["source_key"]),
                    source_id=str(plan.item["id"]), relative_path=note.relative_path,
                    memory_id=plan.item.get("memory_id"),
                    subject_key=str(plan.item.get("subject_key") or ""),
                    content_sha256=str(plan.item.get("content_sha256") or ""),
                    canonical_sha256=str(plan.item.get("canonical_sha256") or ""),
                    file_size=int(plan.item.get("file_size") or 0),
                    file_mtime_ns=plan.item.get("file_mtime_ns"),
                    importer_version=str(plan.item.get("importer_version") or ""),
                    state="conflict", import_id=import_id,
                )
            self.store.record_source_import_job_item(
                job_id=import_id, source_id=(plan.item or {}).get("id"),
                relative_path=note.relative_path, planned_action="conflict",
                result_state="conflict", warning_count=len(note.warnings),
                source_format=str(getattr(note, "format", "markdown"))[:64],
            )
            return self._outcome(note, "conflict", plan.reason)

        old_item = plan.item
        old_memory_id = None
        if old_item is not None:
            old_memory_id = self._live_subject_memory(str(old_item.get("subject_key") or ""))
            old_memory_id = old_memory_id or str(old_item.get("memory_id") or "") or None
        branch = ""
        source_id = str(old_item.get("id")) if old_item is not None else new_id("source")
        source_key = (
            str(old_item.get("source_key"))
            if old_item is not None else stable_source_key(vault_id, note.relative_path)
        )
        if plan.action == "conflict" and policy == "new":
            branch = f"branch:{note.raw_sha256}"
            source_id = new_id("source")
            source_key = stable_source_key(vault_id, note.relative_path, branch=branch)
            old_memory_id = None
        subject_key = f"{self.SUBJECT_PREFIX}:{source_id}"
        imported_at = self._revision_time(old_memory_id)
        metadata = self._metadata(
            note, vault_id=vault_id, source_id=source_id, imported_at=imported_at,
            actor=actor, branch=branch,
        )
        state = "renamed" if plan.action == "renamed" else "imported"

        def finalize(memory_id: str) -> None:
            if old_memory_id:
                old = self.store.get_memory(old_memory_id)
                if old is not None and old.valid_to is None:
                    self.store.close_validity(
                        old_memory_id, at=imported_at,
                        actor=f"{self.SOURCE_KIND}_importer",
                        reason=f"{self.SOURCE_KIND}_source_revision", commit=False,
                    )
                    self.store.retire_memory_graph_state(
                        old_memory_id, at=imported_at, commit=False,
                    )
            if plan.action == "conflict" and policy == "new" and old_item is not None:
                self.store.upsert_source_import_item(
                    vault_id=vault_id, source_key=str(old_item["source_key"]),
                    source_id=str(old_item["id"]), relative_path=note.relative_path,
                    memory_id=old_item.get("memory_id"),
                    subject_key=str(old_item.get("subject_key") or ""),
                    content_sha256=str(old_item.get("content_sha256") or ""),
                    canonical_sha256=str(old_item.get("canonical_sha256") or ""),
                    file_size=int(old_item.get("file_size") or 0),
                    file_mtime_ns=old_item.get("file_mtime_ns"),
                    importer_version=str(old_item.get("importer_version") or ""),
                    state="conflict", import_id=import_id, commit=False,
                )
            self.store.upsert_source_import_item(
                vault_id=vault_id, source_key=source_key, source_id=source_id,
                relative_path=note.relative_path, memory_id=memory_id,
                subject_key=subject_key, content_sha256=note.raw_sha256,
                canonical_sha256=note.canonical_sha256,
                file_size=note.source_size,
                file_mtime_ns=note.source_mtime_ns,
                importer_version=self.IMPORTER_VERSION,
                state=state, import_id=import_id, commit=False,
            )
            result_state = plan.action
            if result_state == "conflict":
                result_state = "imported" if policy == "new" else "updated"
            self.store.record_source_import_job_item(
                job_id=import_id, source_id=source_id,
                relative_path=note.relative_path, planned_action=plan.action,
                result_state=result_state, warning_count=len(note.warnings),
                source_format=str(getattr(note, "format", "markdown"))[:64],
                commit=False,
            )

        if old_memory_id:
            metadata["supersedes"] = [old_memory_id]
        try:
            result = self.engine.remember_with_resolution(
                note.body, workspace_id=workspace_id, repo_id=repo_id,
                session_id=session_id, mtype=memory_type, scope=scope,
                title=note.title, keywords=self._keywords(note), metadata=metadata,
                valid_from=imported_at, subject_key=subject_key,
                claim_kind=self.CLAIM_KIND, resolve_conflicts=False,
                _transactional_finalizer=finalize,
            )
        except Exception:
            if old_item is not None:
                # The source was seen, but its successor could not commit. Preserve
                # the last durable hashes/memory so the next run plans a retry instead
                # of misclassifying the present file as deleted.
                self.store.upsert_source_import_item(
                    vault_id=vault_id, source_key=str(old_item["source_key"]),
                    source_id=str(old_item["id"]), relative_path=note.relative_path,
                    memory_id=old_item.get("memory_id"),
                    subject_key=str(old_item.get("subject_key") or ""),
                    content_sha256=str(old_item.get("content_sha256") or ""),
                    canonical_sha256=str(old_item.get("canonical_sha256") or ""),
                    file_size=int(old_item.get("file_size") or 0),
                    file_mtime_ns=old_item.get("file_mtime_ns"),
                    importer_version=str(old_item.get("importer_version") or ""),
                    state="error", import_id=import_id,
                    last_error="note_import_failed",
                )
            durable_source_id = (
                source_id if self.store.get_source_import(source_id) is not None else None
            )
            self.store.record_source_import_job_item(
                job_id=import_id, source_id=durable_source_id,
                relative_path=note.relative_path, planned_action=plan.action,
                result_state="error", warning_count=len(note.warnings),
                error_code="note_import_failed",
                source_format=str(getattr(note, "format", "markdown"))[:64],
            )
            return self._outcome(note, "error", _SAFE_ERROR)
        action = plan.action
        if action == "conflict":
            action = "imported" if policy == "new" else "updated"
        return self._outcome(note, action, plan.reason, memory_id=str(result["id"]))

    def _revision_time(self, old_memory_id: Optional[str]) -> float:
        stamp = time.time()
        if old_memory_id:
            old = self.store.get_memory(old_memory_id)
            if old is not None and old.valid_from is not None and stamp <= old.valid_from:
                stamp = old.valid_from + 0.000001
        return stamp

    @staticmethod
    def _keywords(note: _ImportNote) -> list[str]:
        values = [*note.tags, *note.aliases]
        out: list[str] = []
        for value in values:
            text = str(value).strip()[:128]
            if text and text not in out:
                out.append(text)
            if len(out) >= 64:
                break
        return out

    @classmethod
    def _metadata(
        cls, note: _ImportNote, *, vault_id: str, source_id: str,
        imported_at: float, actor: str, branch: str,
    ) -> dict:
        folder = str(PurePosixPath(note.relative_path).parent)
        folder = "" if folder == "." else folder
        links = [
            {
                "target": link.target[:500], "display_text": (link.display_text or "")[:500],
                "heading": (link.heading or "")[:500], "block_id": (link.block_id or "")[:200],
                "embedded": bool(link.embedded),
            }
            for link in note.links[:256]
        ]
        obsidian: dict[str, Any] = {
            "vault_id": vault_id,
            "source_id": source_id,
            "relative_path": note.relative_path,
            "folder": folder,
            "original_title": note.title[:1000],
            "title_source": note.title_source,
            "aliases": [str(value)[:200] for value in note.aliases[:64]],
            "tags": [str(value)[:128] for value in note.tags[:64]],
            "dates": {str(key)[:64]: str(value)[:200] for key, value in list(note.dates.items())[:16]},
            "headings": [str(value)[:240] for value in note.headings[:64]],
            "links": links,
            "attachments": [entry.path[:300] for entry in note.attachments[:64]],
            "raw_sha256": note.raw_sha256,
            "canonical_sha256": note.canonical_sha256,
            "file": {"size": int(note.source_size), "mtime_ns": note.source_mtime_ns},
            "importer_version": cls.IMPORTER_VERSION,
            "imported_at": imported_at,
        }
        if branch:
            obsidian["branch"] = branch[:100]
        # The Store enforces a 16 KiB metadata ceiling. Preserve the first parsed
        # values deterministically and record how many were omitted rather than
        # allowing a heavily linked note to fail after preview.
        omitted: dict[str, int] = {}
        for key in ("links", "headings", "attachments", "aliases", "tags"):
            values = obsidian.get(key)
            while isinstance(values, list) and len(
                json.dumps(obsidian, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ) > 13_500 and values:
                before = len(values)
                del values[max(1, before // 2):]
                omitted[key] = omitted.get(key, 0) + before - len(values)
        if omitted:
            obsidian["omitted_counts"] = omitted
        return {
            cls.METADATA_KEY: obsidian,
            "provenance": {
                "source": cls.SOURCE_KIND, "kind": "document_import", "trusted": True,
                "review_state": "approved", "trust_origin": actor,
                "ingress": cls.JOB_KIND,
            },
        }

    def _reconcile_links(
        self, scan: _ImportScan, *, vault_id: str, job_id: Optional[str] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> list[dict]:
        """Resolve derived links in bounded, cancellable, replay-safe batches."""
        items = self.store.list_source_import_items(
            vault_id=vault_id, states=["imported", "unchanged", "renamed", "skipped"],
        )
        memory_by_path = {
            str(item["relative_path"]): str(item["memory_id"])
            for item in items if item.get("memory_id")
        }
        note_by_path = {note.relative_path: note for note in scan.notes}
        exact: dict[str, list[str]] = {}
        names: dict[str, list[str]] = {}

        def add_exact(key: str, path: str) -> None:
            candidates = exact.setdefault(key, [])
            if path not in candidates:
                candidates.append(path)

        for path, note in note_by_path.items():
            add_exact(path.casefold(), path)
            suffixless = str(PurePosixPath(path).with_suffix(""))
            if suffixless != path:
                add_exact(suffixless.casefold(), path)
            keys = {PurePosixPath(path).stem.casefold(), note.title.casefold()}
            keys.update(alias.casefold() for alias in note.aliases)
            for key in keys:
                names.setdefault(key, []).append(path)
        warnings: list[dict] = []
        batch_open = False
        writes_in_batch = 0
        references_seen = 0

        def check_cancel() -> None:
            if job_id is not None:
                self._check_cancel(job_id, cancel_check)
            elif cancel_check is not None and cancel_check():
                raise ObsidianImportCancelled

        def flush() -> None:
            nonlocal batch_open, writes_in_batch
            if batch_open and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.commit()
            batch_open = False
            writes_in_batch = 0

        def retire_ambiguous_links(source_id: str, target_ids: list[str]) -> None:
            nonlocal batch_open, writes_in_batch
            if not target_ids:
                return
            if not batch_open:
                self.store.conn.execute("BEGIN IMMEDIATE")
                batch_open = True
            marks = ",".join("?" for _ in target_ids)
            stamp = time.time()
            cursor = self.store.conn.execute(
                "UPDATE mem_links SET valid_to=?, valid_to_recorded_at=? "
                "WHERE reason=? AND valid_to IS NULL AND expired_at IS NULL "
                "AND ((a=? AND b IN (" + marks + ")) "
                "OR (b=? AND a IN (" + marks + ")))",
                (stamp, stamp, self.LINK_REASON, source_id, *target_ids,
                 source_id, *target_ids),
            )
            writes_in_batch += max(int(cursor.rowcount), 0)

        try:
            for source_path, note in note_by_path.items():
                check_cancel()
                source_id = memory_by_path.get(source_path)
                if not source_id:
                    continue
                source_folder = PurePosixPath(source_path).parent
                for raw_reference, embedded in self._note_references(note):
                    references_seen += 1
                    if references_seen % 64 == 0:
                        check_cancel()
                    paths, ignored = self._reference_paths(source_folder, raw_reference)
                    if ignored:
                        continue
                    candidates: list[str] = []
                    for candidate in paths:
                        key = candidate.casefold()
                        if key in exact:
                            candidates = sorted(set(exact[key]))
                            break
                    if not candidates and paths:
                        candidates = sorted(set(
                            names.get(PurePosixPath(paths[-1]).stem.casefold(), [])
                        ))
                    if len(candidates) != 1:
                        if candidates:
                            retire_ambiguous_links(
                                source_id,
                                [
                                    memory_by_path[path]
                                    for path in candidates
                                    if memory_by_path.get(path)
                                ],
                            )
                        warnings.append(self._outcome(
                            note, "warning",
                            "ambiguous_wikilink" if candidates else "unresolved_wikilink",
                        ))
                        continue
                    target_id = memory_by_path.get(candidates[0])
                    if not target_id or target_id == source_id:
                        continue
                    if not batch_open:
                        self.store.conn.execute("BEGIN IMMEDIATE")
                        batch_open = True
                    self.store.add_link(
                        source_id, target_id, "embeds" if embedded else "references",
                        layer=GraphLayer.SEMANTIC, reason=self.LINK_REASON, commit=False,
                    )
                    writes_in_batch += 1
                    if writes_in_batch >= 128:
                        flush()
        except BaseException:
            if batch_open and self.store.conn.transaction_owned_by_current_thread():
                self.store.conn.rollback()
            raise
        flush()
        return warnings

    def _note_references(self, note: _ImportNote) -> list[tuple[str, bool]]:
        """Return source references while preserving Obsidian attachment semantics."""
        attachments = {str(item.path) for item in note.attachments}
        result: list[tuple[str, bool]] = []
        seen: set[tuple[str, bool]] = set()
        for link in note.links:
            value = (str(link.target), bool(link.embedded))
            if value[0] in attachments and not self.LINK_IMPORTED_ATTACHMENTS:
                continue
            if value not in seen:
                seen.add(value)
                result.append(value)
        if self.LINK_IMPORTED_ATTACHMENTS:
            for attachment in note.attachments:
                value = (str(attachment.path), True)
                if value not in seen:
                    seen.add(value)
                    result.append(value)
        return result

    @staticmethod
    def _reference_paths(
        source_folder: PurePosixPath, target: str,
    ) -> tuple[list[str], bool]:
        """Resolve a source reference without filesystem access or root traversal."""
        raw = str(target or "").strip()
        if not raw or raw.startswith("#"):
            return [], True
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            return [], True
        decoded = unquote(parsed.path).replace("\\", "/")
        if not decoded:
            return [], True

        values: list[str] = []
        raw_root = decoded.lstrip("/")
        raw_candidates = (
            [raw_root] if decoded.startswith("/")
            else [posixpath.join(str(source_folder), raw_root), raw_root]
        )
        for candidate in raw_candidates:
            normalized = posixpath.normpath(candidate)
            if (
                normalized in {"", ".", ".."}
                or normalized.startswith("../")
                or normalized.startswith("/")
            ):
                continue
            if normalized not in values:
                values.append(normalized)
        return values, False

    @staticmethod
    def _outcome(
        note: _ImportNote, status: str, reason: str, *, memory_id: str = "",
    ) -> dict:
        row = {
            "relative_path": note.relative_path, "status": status, "action": status,
            "reason": reason, "warnings": list(note.warnings[:20]),
            "format": str(getattr(note, "format", "markdown")),
            "media_type": str(getattr(note, "media_type", "text/markdown")),
        }
        if memory_id:
            row["memory_id"] = memory_id
        return row

    def _report(
        self, plans: list[_Plan], missing: list[dict], scan: _ImportScan, *,
        state: str, vault_id: Optional[str], workspace_id: Optional[str],
        repo_id: Optional[str], session_id: Optional[str], scope: Scope,
        memory_type: MemoryType, policy: str, vault_label: str,
        attachment_manifest: Optional[list[dict]],
    ) -> dict:
        files = [self._preview_row(plan) for plan in plans]
        files.extend({
            "relative_path": issue.relative_path, "status": "rejected", "action": "rejected",
            "reason": issue.reason, "warnings": [],
        } for issue in scan.rejected)
        files.extend({
            "relative_path": issue.relative_path, "status": "skipped", "action": "skipped",
            "reason": issue.reason, "warnings": [],
        } for issue in scan.skipped)
        files.extend({
            "relative_path": str(item.get("relative_path") or ""), "status": "missing",
            "action": "missing", "reason": "source_not_seen", "warnings": [],
        } for item in missing)
        return self._report_payload(
            files, scan, state=state, vault_id=vault_id,
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scope=scope, memory_type=memory_type, policy=policy,
            vault_label=vault_label, attachment_manifest=attachment_manifest,
        )

    def _final_report(
        self, plans: list[_Plan], outcomes: list[dict], missing: list[dict],
        scan: _ImportScan, *, pending_missing: list[dict], state: str,
        vault_id: str, job_id: str,
        import_id: str, workspace_id: str, repo_id: Optional[str],
        session_id: Optional[str], scope: Scope, memory_type: MemoryType,
        policy: str, vault_label: str, attachment_manifest: Optional[list[dict]],
    ) -> dict:
        files = list(outcomes)
        files.extend({
            "relative_path": issue.relative_path, "status": "rejected", "action": "rejected",
            "reason": issue.reason, "warnings": [],
        } for issue in scan.rejected)
        files.extend({
            "relative_path": issue.relative_path, "status": "skipped", "action": "skipped",
            "reason": issue.reason, "warnings": [],
        } for issue in scan.skipped)
        files.extend({
            "relative_path": str(item.get("relative_path") or ""), "status": "missing",
            "action": "missing", "reason": "source_not_seen", "warnings": [],
        } for item in missing)
        files.extend({
            "relative_path": str(item.get("relative_path") or ""), "status": "pending",
            "action": "pending", "reason": "missing_check_deferred", "warnings": [],
        } for item in pending_missing)
        report = self._report_payload(
            files, scan, state=state, vault_id=vault_id,
            workspace_id=workspace_id, repo_id=repo_id, session_id=session_id,
            scope=scope, memory_type=memory_type, policy=policy,
            vault_label=vault_label, attachment_manifest=attachment_manifest,
        )
        report.update({"job_id": job_id, "import_id": import_id})
        return report

    @staticmethod
    def _preview_row(plan: _Plan) -> dict:
        note = plan.note
        return {
            "relative_path": note.relative_path, "status": plan.action,
            "action": plan.action, "reason": plan.reason, "title": note.title,
            "folder": (
                "" if str(PurePosixPath(note.relative_path).parent) == "."
                else str(PurePosixPath(note.relative_path).parent)
            ),
            "aliases": note.aliases, "tags": note.tags, "headings": note.headings,
            "links": [link.__dict__ for link in note.links],
            "attachments": [entry.__dict__ for entry in note.attachments],
            "warnings": note.warnings,
            "format": str(getattr(note, "format", "markdown")),
            "media_type": str(getattr(note, "media_type", "text/markdown")),
        }

    def _report_payload(
        self, files: list[dict], scan: _ImportScan, *, state: str,
        vault_id: Optional[str], workspace_id: Optional[str], repo_id: Optional[str],
        session_id: Optional[str], scope: Scope, memory_type: MemoryType,
        policy: str, vault_label: str, attachment_manifest: Optional[list[dict]],
    ) -> dict:
        counts: dict[str, int] = {}
        for row in files:
            status = str(row.get("status") or "reported")
            counts[status] = counts.get(status, 0) + 1
        counts[self.COUNT_KEY] = len(scan.notes) + len(scan.rejected)
        folders = sorted({
            str(PurePosixPath(note.relative_path).parent)
            for note in scan.notes if str(PurePosixPath(note.relative_path).parent) != "."
        })
        tags = sorted({tag for note in scan.notes for tag in note.tags})
        formats: dict[str, int] = {}
        for note in scan.notes:
            name = str(getattr(note, "format", "markdown") or "unknown")
            formats[name] = formats.get(name, 0) + 1
        return {
            "state": state, "status": state, "vault_id": vault_id,
            "vault_label": str(vault_label or "")[:200],
            "source_id": vault_id,
            "source_label": str(vault_label or "")[:200],
            "source_kind": self.SOURCE_KIND,
            "target": {
                "workspace_id": workspace_id, "repo_id": repo_id,
                "session_id": session_id, "scope": scope.value,
                "memory_type": memory_type.value,
            },
            "on_conflict": policy, "counts": counts,
            "summary": {
                "folders": folders[:500], "tags": tags[:500],
                "aliases": sum(len(note.aliases) for note in scan.notes),
                "wikilinks": sum(len(note.links) for note in scan.notes),
                "attachments": sum(len(note.attachments) for note in scan.notes),
                "attachment_manifest": len(attachment_manifest or []),
                "warnings": sum(len(note.warnings) for note in scan.notes),
                "skipped_paths": len(scan.skipped),
                "formats": formats,
            },
            "files": files,
        }

    def _create_job(
        self, workspace_id: str, repo_id: Optional[str], *, session_id: Optional[str], total: int,
        policy: str, scope: Scope, memory_type: MemoryType,
    ) -> str:
        job_id = new_id("job")
        stamp = time.time()
        self.store.conn.execute(
            "INSERT INTO jobs(id, workspace_id, repo_id, session_id, kind, state, dry_run, total_items, "
            "processed_items, counts, errors, request, cancel_requested, created_at, started_at) "
            "VALUES (?,?,?,?,?,'running',0,?,0,'{}','[]',?,0,?,?)",
            (
                job_id, workspace_id, repo_id, session_id, self.JOB_KIND, int(total),
                json.dumps({
                    "scope": scope.value, "memory_type": memory_type.value,
                    "on_conflict": policy,
                }, sort_keys=True, separators=(",", ":")),
                stamp, stamp,
            ),
        )
        self.store.conn.commit()
        return job_id

    def _check_cancel(
        self, job_id: str, cancel_check: Optional[Callable[[], bool]],
    ) -> None:
        if cancel_check is not None and cancel_check():
            raise ObsidianImportCancelled
        row = self.store.conn.execute(
            "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,),
        ).fetchone()
        if row is not None and bool(row["cancel_requested"]):
            raise ObsidianImportCancelled

    def _update_job_progress(self, job_id: str, processed: int, outcomes: list[dict]) -> None:
        counts: dict[str, int] = {}
        for row in outcomes:
            status = str(row.get("status") or "reported")
            counts[status] = counts.get(status, 0) + 1
        self.store.conn.execute(
            "UPDATE jobs SET processed_items=?, counts=?, heartbeat_at=? WHERE id=?",
            (processed, json.dumps(counts, sort_keys=True, separators=(",", ":")), time.time(), job_id),
        )
        self.store.conn.commit()

    def _finish_job(self, job_id: str, state: str, report: dict) -> None:
        errors = [
            {"status": row.get("status"), "reason": row.get("reason")}
            for row in report.get("files", [])
            if row.get("status") in {"error", "conflict", "rejected"}
        ][:1000]
        self.store.conn.execute(
            "UPDATE jobs SET state=?, processed_items=?, counts=?, errors=?, finished_at=?, "
            "heartbeat_at=? WHERE id=?",
            (
                state, sum(report.get("counts", {}).get(key, 0) for key in (
                    "imported", "updated", "renamed", "skipped", "conflict", "error",
                    "rejected",
                )),
                json.dumps(report.get("counts", {}), sort_keys=True, separators=(",", ":")),
                json.dumps(errors, sort_keys=True, separators=(",", ":")),
                time.time(), time.time(), job_id,
            ),
        )
        self.store.conn.commit()

    def _record_receipt(
        self, report: dict, *, workspace_id: str, repo_id: Optional[str], actor: str,
    ) -> None:
        counts = report.get("counts", {})
        summary = report.get("summary", {})
        self.store.record_receipt(
            self.RECEIPT_OPERATION, workspace_id=workspace_id, repo_id=repo_id or "",
            actor=actor, target_count=int(counts.get(self.COUNT_KEY, 0)),
            status=str(report.get("state") or "partial"),
            metadata={
                "files_imported": int(counts.get("imported", 0)),
                "files_updated": int(counts.get("updated", 0)),
                "files_renamed": int(counts.get("renamed", 0)),
                "files_skipped": int(counts.get("skipped", 0)),
                "files_rejected": int(counts.get("rejected", 0)),
                "files_missing": int(counts.get("missing", 0)),
                "files_errored": int(counts.get("error", 0)),
                "conflicts": int(counts.get("conflict", 0)),
                "warnings": int(summary.get("warnings", 0)),
                "attachments": int(summary.get("attachments", 0)),
                "wikilinks": int(summary.get("wikilinks", 0)),
                "aliases": int(summary.get("aliases", 0)),
                "tags": len(summary.get("tags", [])),
            },
        )


__all__ = [
    "ObsidianImportCancelled", "ObsidianImporter", "scan_obsidian_upload",
    "stable_source_key",
]
