"""Previewed, lossless relocation of bounded local memory components.

The service supplies authorization and an owned transaction. This module only uses
the canonical store: vectors, text, temporal versions and historical receipts stay
intact. Dependencies that need a separate migration protocol fail closed.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from . import ids
from .interfaces import MemoryRecord
from .mutations import memory_version

MAX_MOVE_MEMORIES = 500
MAX_MOVE_SCAN = 25000
_MEMORY_ID = re.compile(r"^mem_[A-Za-z0-9_-]+$")


class RelocationStore(Protocol):
    conn: Any

    def get_memory(self, memory_id: str) -> Optional[MemoryRecord]: ...
    def advance_memory_modified_hlc(self, memory_id: str, *, commit: bool = True) -> str: ...
    def audit(self, actor: str, action: str, target: str, detail: str = "") -> Any: ...


def _json(raw: Any) -> Any:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Repair malformed memory metadata before moving it.") from exc


def _references(value: Any) -> set[str]:
    """Exact typed references, including nested evidence and legacy provenance."""
    found: set[str] = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and _MEMORY_ID.fullmatch(item):
            found.add(item)
    return found


def _sync_origin(value: Any) -> bool:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get("synced_from_device") or item.get("source") in ("sync", "sync_conflict"):
                return True
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


def _endpoints(edge: dict, mapping: dict) -> tuple[str, str]:
    source, target = mapping[edge["src"]], mapping[edge["dst"]]
    if edge["relation"] in {"co_occurs", "related", "associated_with"} and target < source:
        source, target = target, source
    return source, target


def _rows(conn: Any, sql: str, args: tuple = ()) -> list[dict]:
    rows = [dict(row) for row in conn.execute(sql, args).fetchmany(MAX_MOVE_SCAN + 1)]
    if len(rows) > MAX_MOVE_SCAN:
        raise ValueError("This move exceeds the review limit; organize a smaller workspace first.")
    return rows


@dataclass
class MovePlan:
    source_id: str
    target_id: str
    requested_ids: list[str]
    records: list[MemoryRecord] = field(default_factory=list)
    blockers: list[dict] = field(default_factory=list)
    repos: list[dict] = field(default_factory=list)
    sessions: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    commands: list[dict] = field(default_factory=list)
    command_sources: list[dict] = field(default_factory=list)
    entities: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    incidences: list[dict] = field(default_factory=list)
    preview_token: str = ""

    def block(self, code: str, message: str) -> None:
        if not any(item["code"] == code for item in self.blockers):
            self.blockers.append({"code": code, "message": message})

    def public(self, source: str, target: str) -> dict:
        return {
            "source": source, "target": target,
            "requested_ids": self.requested_ids,
            "memory_ids": [record.id for record in self.records],
            "count": len(self.records),
            "related_count": len(self.records) - len(self.requested_ids),
            "memories": [{"id": record.id, "title": record.title or record.content[:88],
                          "related": record.id not in self.requested_ids}
                         for record in self.records],
            "sessions": len(self.sessions), "graph_edges": len(self.edges),
            "repos": [row["name"] for row in self.repos],
            "blockers": self.blockers, "can_move": not self.blockers,
            "preview_token": self.preview_token if not self.blockers else "",
        }


def prepare_move(store: RelocationStore, source_id: str, target_id: str,
                 requested_ids: list[str]) -> MovePlan:
    """Read a complete dependency component; the caller authorizes every member."""
    c = store.conn
    plan = MovePlan(source_id, target_id, requested_ids)
    headers = _rows(c, "SELECT id, repo_id, session_id, metadata, provenance FROM memories "
                    "WHERE workspace_id=? ORDER BY id", (source_id,))
    by_id = {row["id"]: row for row in headers}
    if any(mid not in by_id for mid in requested_ids):
        raise ValueError("Every selected memory must belong to the source workspace.")
    adjacent: dict[str, set[str]] = {mid: set() for mid in by_id}

    def connect(members: set[str]) -> None:
        if not members:
            return
        first = min(members)
        for mid in members:
            adjacent.setdefault(first, set()).add(mid)
            adjacent.setdefault(mid, set()).add(first)

    session_members: dict[str, set[str]] = {}
    for row in headers:
        connect({row["id"]} | _references(_json(row["metadata"]))
                | _references(_json(row["provenance"])))
        if row["session_id"]:
            session_members.setdefault(row["session_id"], set()).add(row["id"])
    for members in session_members.values():
        connect(members)
    links = _rows(c, "SELECT l.* FROM mem_links l WHERE l.a IN "
                  "(SELECT id FROM memories WHERE workspace_id=?) OR l.b IN "
                  "(SELECT id FROM memories WHERE workspace_id=?) ORDER BY l.rowid",
                  (source_id, source_id))
    for link in links:
        connect({link["a"], link["b"]})
    edges = _rows(c, "SELECT * FROM edges WHERE workspace_id=? ORDER BY id", (source_id,))
    supports = _rows(c, "SELECT s.* FROM edge_supports s WHERE s.edge_id IN "
                     "(SELECT id FROM edges WHERE workspace_id=?) OR s.memory_id IN "
                     "(SELECT id FROM memories WHERE workspace_id=?) ORDER BY s.id",
                     (source_id, source_id))
    edge_members: dict[str, set[str]] = {}
    for support in supports:
        edge_members.setdefault(support["edge_id"], set()).add(support["memory_id"])
    for edge in edges:
        edge_members.setdefault(edge["id"], set()).update(_references(_json(edge["provenance"])))
    for members in edge_members.values():
        connect(members)
    commands = _rows(c, "SELECT cmd.result_id, src.source_id FROM memory_commands cmd "
                     "JOIN memory_command_sources src ON src.workspace_id=cmd.workspace_id "
                     "AND src.operation_id=cmd.operation_id WHERE cmd.workspace_id=? "
                     "ORDER BY cmd.sequence, src.source_id", (source_id,))
    for command in commands:
        connect({command["result_id"], command["source_id"]})

    selected: set[str] = set()
    pending = list(requested_ids)
    while pending:
        mid = pending.pop()
        if mid in selected:
            continue
        if mid not in by_id:
            plan.block("external_history", "Related history is missing or belongs to another "
                       "workspace. Repair that history before moving this selection.")
            continue
        selected.add(mid)
        if len(selected) > MAX_MOVE_MEMORIES:
            raise ValueError(f"Related history exceeds the {MAX_MOVE_MEMORIES}-memory move limit.")
        pending.extend(adjacent.get(mid, set()) - selected)
    for mid in sorted(selected):
        record = store.get_memory(mid)
        if record is None:
            raise ValueError("Memory changed during preview; refresh and try again.")
        plan.records.append(record)
        if _sync_origin(record.metadata) or _sync_origin(record.provenance):
            plan.block("synced_memory", "This selection contains synced memories. Use a sync-aware "
                       "workspace migration so existing peers retain consistent ownership.")
        if any(key in record.metadata for key in ("document", "obsidian")):
            plan.block("imported_document", "Imported documents must stay with their source "
                       "collection. Re-import the collection into the intended workspace.")
    marks = ",".join("?" for _ in selected)
    mids = tuple(sorted(selected))
    attachments: dict[str, list[dict]] = {}
    plan.commands = _rows(c, f"SELECT * FROM memory_commands WHERE result_id IN ({marks}) "
                          f"OR (workspace_id,operation_id) IN (SELECT workspace_id,operation_id "
                          f"FROM memory_command_sources WHERE source_id IN ({marks})) "
                          "ORDER BY sequence", mids + mids)
    for command in plan.commands:
        sources = _rows(c, "SELECT * FROM memory_command_sources WHERE workspace_id=? "
                        "AND operation_id=? ORDER BY source_id",
                        (command["workspace_id"], command["operation_id"]))
        plan.command_sources.extend(sources)
        if (command["workspace_id"] != source_id or command["result_id"] not in selected
                or any(row["source_id"] not in selected for row in sources)):
            plan.block("external_command", "A correction or review operation has history outside "
                       "this selection. Repair that history before moving it.")
        if c.execute("SELECT 1 FROM memory_commands WHERE workspace_id=? AND operation_id=?",
                     (target_id, command["operation_id"])).fetchone():
            plan.block("target_operation_conflict", "The destination already contains a correction "
                       "or review with the same operation ID. Choose another destination.")
    for table, message, code in (
        ("memory_sync_exports", "Previously synced memories need a sync-aware workspace migration.",
         "synced_memory"),
        ("memory_tombstones", "This selection contains an erasure marker and cannot be moved.",
         "erased_memory"),
        ("source_imports", "Imported documents must stay with their source collection. "
         "Re-import the collection into the intended workspace.", "imported_document"),
        ("code_memory_links", "This selection is linked to indexed code. Move the whole workspace "
         "to retain its code graph, or choose memories without code links.", "code_links"),
    ):
        attachments[table] = _rows(c, f"SELECT * FROM {table} WHERE memory_id IN ({marks})", mids)
        if attachments[table]:
            plan.block(code, message)

    session_ids = sorted({record.session_id for record in plan.records if record.session_id})
    for sid in session_ids:
        row = c.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None or row["workspace_id"] != source_id:
            raise ValueError("A memory has an invalid session. Repair its ownership before moving.")
        session = dict(row)
        plan.sessions.append(session)
        if session["status"] not in ("summarized", "consolidated"):
            plan.block("active_session", "End active sessions before moving their memories. "
                       "All memories and events in each closed session move together.")
        jobs = _rows(c, "SELECT * FROM jobs WHERE session_id=? ORDER BY id", (sid,))
        vaults = _rows(c, "SELECT * FROM source_vaults WHERE session_id=? ORDER BY id", (sid,))
        if jobs or vaults:
            plan.block("session_jobs", "A related session owns import or maintenance jobs. "
                       "Use a whole-workspace operation to preserve that job history.")
        attachments["session_jobs:" + sid] = jobs + vaults
        plan.events.extend(_rows(c, "SELECT * FROM events WHERE session_id=? ORDER BY id", (sid,)))
        if any(event["workspace_id"] != source_id for event in plan.events):
            raise ValueError("A session event has inconsistent workspace ownership.")
        external = _references(_json(session.get("handoff")))
        for event in plan.events:
            external.update(_references(_json(event.get("refs"))))
        if external - selected:
            plan.block("session_references", "Session handoff or events reference other memories. "
                       "Include their related history before moving the session.")

    moving_event_ids = {event["id"] for event in plan.events}
    incoming_events = [event for event in _rows(c, "SELECT * FROM events WHERE workspace_id=? "
                                              "ORDER BY id", (source_id,))
                       if _references(_json(event.get("refs"))) & selected]
    if any(event["id"] not in moving_event_ids for event in incoming_events):
        plan.block("external_events", "Events outside these closed sessions reference the selected "
                   "memories. Use a whole-workspace operation to keep that event history together.")

    plan.edges = [edge for edge in edges if edge_members[edge["id"]] & selected]
    edge_ids = {edge["id"] for edge in plan.edges}
    selected_supports = [s for s in supports if s["memory_id"] in selected]
    if any(s["edge_id"] not in edge_ids for s in selected_supports):
        plan.block("external_graph", "Related graph evidence belongs to another workspace. "
                   "Repair that graph before moving these memories.")
    plan.incidences = _rows(c, f"SELECT * FROM memory_entities WHERE memory_id IN ({marks}) "
                           "ORDER BY id", mids)
    entity_ids = {row["entity_id"] for row in plan.incidences}
    for edge in plan.edges:
        entity_ids.update((edge["src"], edge["dst"]))
    pending_entities = sorted(entity_ids)
    seen_entities: set[str] = set()
    while pending_entities:
        eid = pending_entities.pop()
        if eid in seen_entities:
            continue
        seen_entities.add(eid)
        if len(seen_entities) > MAX_MOVE_SCAN:
            raise ValueError("Related entity history exceeds the move review limit.")
        row = c.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
        if row is None or row["workspace_id"] != source_id:
            plan.block("external_graph", "Related graph entities have inconsistent ownership. "
                       "Repair the graph before moving these memories.")
        else:
            plan.entities.append(dict(row))
            if row["canonical_id"] and row["canonical_id"] != eid:
                pending_entities.append(row["canonical_id"])
    plan.entities.sort(key=lambda entity: entity["id"])

    repo_ids = {record.repo_id for record in plan.records if record.repo_id}
    for row in plan.sessions + plan.events + plan.entities + plan.edges + plan.incidences:
        if row.get("repo_id"):
            repo_ids.add(row["repo_id"])
    for rid in sorted(repo_ids):
        row = c.execute("SELECT * FROM repos WHERE id=?", (rid,)).fetchone()
        if row is None or row["workspace_id"] != source_id:
            raise ValueError("Related data has inconsistent project ownership.")
        repo = dict(row)
        target = c.execute("SELECT * FROM repos WHERE workspace_id=? AND name=?",
                           (target_id, repo["name"])).fetchone()
        repo["target"] = dict(target) if target else None
        plan.repos.append(repo)
    repo_targets = {row["id"]: row["target"]["id"] if row["target"] else "new:" + row["id"]
                    for row in plan.repos}
    for entity in plan.entities:
        rid = repo_targets.get(entity["repo_id"])
        # Preserve distinct source aliases. Reusing every normalized-name match
        # would collapse their incidence rows and can violate the live uniqueness
        # constraints even though the original source graph was valid.
        target = c.execute("SELECT * FROM entities WHERE workspace_id=? AND repo_id IS ? "
                           "AND name=? AND etype IS ? ORDER BY id LIMIT 1",
                           (target_id, rid, entity["name"], entity["etype"])).fetchone()
        entity["target"] = dict(target) if target else None
    entity_targets = {row["id"]: row["target"]["id"] if row["target"] else "new:" + row["id"]
                      for row in plan.entities}
    target_ancestors: dict[str, dict] = {}
    source_entities = {row["id"]: row for row in plan.entities}

    def existing_root(entity: dict) -> str:
        seen: set[str] = set()
        current = entity
        while current.get("canonical_id") and current["canonical_id"] != current["id"]:
            if current["id"] in seen:
                raise ValueError("Repair the destination's cyclic entity history before moving.")
            seen.add(current["id"])
            row = c.execute("SELECT * FROM entities WHERE id=?", (current["canonical_id"],)).fetchone()
            if row is None or row["workspace_id"] != target_id:
                raise ValueError("Repair the destination's entity ownership before moving.")
            current = dict(row)
            target_ancestors[current["id"]] = current
        return current["id"]

    roots: dict[str, str] = {}

    def planned_root(eid: str, visiting: set[str]) -> str:
        if eid in roots:
            return roots[eid]
        if eid in visiting or eid not in source_entities or len(visiting) > 64:
            raise ValueError("Repair incomplete or cyclic entity history before moving.")
        entity = source_entities[eid]
        parent = entity.get("canonical_id")
        if entity["target"]:
            root = existing_root(entity["target"])
        elif parent and parent != eid:
            root = planned_root(parent, visiting | {eid})
        else:
            root = "new:" + eid
        roots[eid] = root
        return root

    for entity in plan.entities:
        entity["root"] = planned_root(entity["id"], set())
        parent = entity.get("canonical_id")
        if entity["target"] and parent and parent != entity["id"]:
            if entity["root"] != planned_root(parent, set()):
                plan.block("target_graph_conflict", "The destination groups an existing entity "
                           "differently. Use a whole-workspace merge to reconcile its history.")
        if not entity["target"] and entity["root"] == "new:" + entity["id"]:
            # A root with a differently spelled existing normalized name would
            # violate the target's uniqueness rule. Do not silently rewrite aliases.
            collision = c.execute("SELECT * FROM entities WHERE workspace_id=? AND repo_id IS ? "
                                  "AND normalized_name=? AND etype IS ? AND canonical_id=id",
                                  (target_id, repo_targets.get(entity["repo_id"]),
                                   entity["normalized_name"], entity["etype"])).fetchone()
            if collision and entity["normalized_name"]:
                target_ancestors[collision["id"]] = dict(collision)
                plan.block("target_graph_conflict", "The destination already groups a matching "
                           "entity under another name. Reconcile that graph before moving.")
    target_conflicts = []
    incoming_keys: set[tuple] = set()
    for edge in plan.edges:
        if edge["valid_to"] is not None or edge["expired_at"] is not None:
            continue
        if edge["src"] not in entity_targets or edge["dst"] not in entity_targets:
            continue  # already blocked for invalid graph ownership
        start, end = _endpoints(edge, entity_targets)
        key = (repo_targets.get(edge["repo_id"]), start, end, edge["relation"], edge["layer"])
        if key in incoming_keys:
            plan.block("target_graph_conflict", "Related graph relations would collide in the "
                       "destination. Use a whole-workspace merge to reconcile their evidence.")
        incoming_keys.add(key)
        collision = c.execute("SELECT * FROM edges WHERE workspace_id=? AND repo_id IS ? "
                              "AND src=? AND dst=? AND relation=? AND layer=? "
                              "AND valid_to IS NULL AND expired_at IS NULL LIMIT 1",
                              (target_id, repo_targets.get(edge["repo_id"]),
                               start, end,
                               edge["relation"], edge["layer"])).fetchone()
        if collision:
            target_conflicts.append(dict(collision))
            plan.block("target_graph_conflict", "The destination already has matching graph "
                       "relations. Use a whole-workspace merge to reconcile their evidence.")
    for record in plan.records:
        if (record.scope.value == "session" or not record.subject_key
                or record.valid_to is not None or record.expired_at is not None):
            continue
        collision = c.execute("SELECT id, metadata, provenance, modified_hlc FROM memories "
                              "WHERE workspace_id=? AND repo_id IS ? AND scope=? AND mtype=? "
                              "AND subject_key=? AND claim_kind=? AND valid_to IS NULL "
                              "AND expired_at IS NULL LIMIT 1",
                              (target_id, repo_targets.get(record.repo_id), record.scope.value,
                               record.mtype.value, record.subject_key, record.claim_kind)).fetchone()
        if collision:
            target_conflicts.append(dict(collision))
            plan.block("target_claim_conflict", "The destination already contains a claim with "
                       "the same key. Review that conflict before moving these memories.")
    ownership = _rows(c, "SELECT * FROM workspaces WHERE id IN (?,?) ORDER BY id",
                      (source_id, target_id))
    plan.preview_token = "move1:" + _digest({
        "ownership": ownership, "requested": requested_ids,
        "versions": [[record.id, memory_version(record)] for record in plan.records],
        "links": [link for link in links if link["a"] in selected or link["b"] in selected],
        "commands": plan.commands, "command_sources": plan.command_sources,
        "incoming_events": incoming_events,
        "repos": plan.repos, "sessions": plan.sessions, "events": plan.events,
        "entities": plan.entities, "target_ancestors": target_ancestors,
        "edges": plan.edges, "supports": selected_supports,
        "incidences": plan.incidences, "attachments": attachments,
        "conflicts": target_conflicts, "blockers": plan.blockers,
    })
    return plan


def apply_move(store: RelocationStore, plan: MovePlan, *, actor: str) -> None:
    """Apply an authorized, revalidated plan inside the service's writer reservation."""
    if plan.blockers:
        raise ValueError("Resolve the preview blockers before moving memories.")
    c = store.conn
    now = time.time()
    repo_map: dict[str, str] = {}
    for repo in plan.repos:
        if repo["target"]:
            repo_map[repo["id"]] = repo["target"]["id"]
        else:
            rid = ids.new_id("repo")
            repo_map[repo["id"]] = rid
            # Host routing and indexed-code settings are not portable with a memory subset.
            c.execute("INSERT INTO repos(id,workspace_id,name,created_at,settings) VALUES(?,?,?,?,?)",
                      (rid, plan.target_id, repo["name"], now, "{}"))
    for session in plan.sessions:
        c.execute("UPDATE sessions SET workspace_id=?,repo_id=? WHERE id=?",
                  (plan.target_id, repo_map.get(session["repo_id"]), session["id"]))
    for event in plan.events:
        c.execute("UPDATE events SET workspace_id=?,repo_id=? WHERE id=?",
                  (plan.target_id, repo_map.get(event["repo_id"]), event["id"]))
    entity_map = {entity["id"]: entity["target"]["id"] if entity["target"] else ids.new_id("entity")
                  for entity in plan.entities}
    for entity in plan.entities:
        if entity["target"]:
            continue
        eid = entity_map[entity["id"]]
        root = entity["root"]
        canonical = entity_map[root[4:]] if root.startswith("new:") else root
        c.execute("INSERT INTO entities(id,workspace_id,repo_id,name,etype,canonical_id,"
                  "normalized_name,canonical_method,canonical_confidence,created_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (eid, plan.target_id, repo_map.get(entity["repo_id"]), entity["name"],
                   entity["etype"], canonical,
                   entity["normalized_name"], entity["canonical_method"],
                   entity["canonical_confidence"], entity["created_at"]))
    for edge in plan.edges:
        start, end = _endpoints(edge, entity_map)
        c.execute("UPDATE edges SET workspace_id=?,repo_id=?,src=?,dst=? WHERE id=?",
                  (plan.target_id, repo_map.get(edge["repo_id"]), start, end, edge["id"]))
    for incidence in plan.incidences:
        c.execute("UPDATE memory_entities SET workspace_id=?,repo_id=?,entity_id=? WHERE id=?",
                  (plan.target_id, repo_map.get(incidence["repo_id"]),
                   entity_map[incidence["entity_id"]], incidence["id"]))
    for record in plan.records:
        c.execute("UPDATE memories SET workspace_id=?,repo_id=? WHERE id=?",
                  (plan.target_id, repo_map.get(record.repo_id or ""), record.id))
        store.advance_memory_modified_hlc(record.id, commit=False)
        store.audit(actor, "workspace_move", record.id, json.dumps({
            "operation": plan.preview_token, "source_workspace": plan.source_id,
            "target_workspace": plan.target_id, "source_repo": record.repo_id,
            "target_repo": repo_map.get(record.repo_id or ""),
        }, sort_keys=True))
    # Keep operation identities and ordering with their history. Remove only the
    # child keys while updating the parent so immediate foreign keys remain valid.
    for row in plan.command_sources:
        c.execute("DELETE FROM memory_command_sources WHERE source_id=?", (row["source_id"],))
    for command in plan.commands:
        result = store.get_memory(command["result_id"])
        if result is None:
            raise ValueError("A correction result disappeared during the move.")
        c.execute("UPDATE memory_commands SET workspace_id=?,result_version=? WHERE sequence=?",
                  (plan.target_id, memory_version(result), command["sequence"]))
    for row in plan.command_sources:
        c.execute("INSERT INTO memory_command_sources(source_id,workspace_id,operation_id) VALUES(?,?,?)",
                  (row["source_id"], plan.target_id, row["operation_id"]))
    for wid in (plan.source_id, plan.target_id):
        c.execute("INSERT INTO graph_index_state(workspace_id,generation,state,updated_at) "
                  "VALUES(?,1,'ready',?) ON CONFLICT(workspace_id) DO UPDATE SET "
                  "generation=graph_index_state.generation+1,updated_at=excluded.updated_at",
                  (wid, now))
