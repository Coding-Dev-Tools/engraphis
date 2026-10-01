"""Previewed, lossless relocation of bounded local memory components.

The service supplies authorization and an owned transaction. This planner uses
domain operations from interfaces.py; persistence owns SQL and preserves vectors,
text, temporal versions and receipts. Dependencies requiring migration fail closed.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .interfaces import MovePlan, RelocationStore
from .mutations import memory_version

MAX_MOVE_MEMORIES = 500
MAX_MOVE_SCAN = 25000
_MEMORY_ID = re.compile(r"^mem_[A-Za-z0-9_-]+$")


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


def prepare_move(store: RelocationStore, source_id: str, target_id: str,
                 requested_ids: list[str]) -> MovePlan:
    """Read a complete dependency component; the caller authorizes every member."""
    plan = MovePlan(source_id, target_id, requested_ids)
    dependencies = store.relocation_dependencies(source_id, limit=MAX_MOVE_SCAN)
    headers = dependencies.memories
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
    links = dependencies.links
    for link in links:
        connect({link["a"], link["b"]})
    edges = dependencies.edges
    supports = dependencies.supports
    edge_members: dict[str, set[str]] = {}
    for support in supports:
        edge_members.setdefault(support["edge_id"], set()).add(support["memory_id"])
    for edge in edges:
        edge_members.setdefault(edge["id"], set()).update(_references(_json(edge["provenance"])))
    for members in edge_members.values():
        connect(members)
    commands = dependencies.command_links
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
    history = store.relocation_history(sorted(selected), limit=MAX_MOVE_SCAN)
    attachments = history.attachments
    plan.commands = history.commands
    for command in plan.commands:
        sources = history.command_sources[(command["workspace_id"], command["operation_id"])]
        plan.command_sources.extend(sources)
        if (command["workspace_id"] != source_id or command["result_id"] not in selected
                or any(row["source_id"] not in selected for row in sources)):
            plan.block("external_command", "A correction or review operation has history outside "
                       "this selection. Repair that history before moving it.")
        if store.relocation_operation_exists(target_id, command["operation_id"]):
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
        if attachments.get(table):
            plan.block(code, message)

    session_ids = sorted({record.session_id for record in plan.records if record.session_id})
    for sid in session_ids:
        session_history = store.relocation_session_history(sid, limit=MAX_MOVE_SCAN)
        row = session_history.session
        if row is None or row["workspace_id"] != source_id:
            raise ValueError("A memory has an invalid session. Repair its ownership before moving.")
        session = dict(row)
        plan.sessions.append(session)
        if session["status"] not in ("summarized", "consolidated"):
            plan.block("active_session", "End active sessions before moving their memories. "
                       "All memories and events in each closed session move together.")
        jobs = session_history.jobs
        vaults = session_history.source_vaults
        if jobs or vaults:
            plan.block("session_jobs", "A related session owns import or maintenance jobs. "
                       "Use a whole-workspace operation to preserve that job history.")
        attachments["session_jobs:" + sid] = jobs + vaults
        plan.events.extend(session_history.events)
        if any(event["workspace_id"] != source_id for event in plan.events):
            raise ValueError("A session event has inconsistent workspace ownership.")
        external = _references(_json(session.get("handoff")))
        for event in plan.events:
            external.update(_references(_json(event.get("refs"))))
        if external - selected:
            plan.block("session_references", "Session handoff or events reference other memories. "
                       "Include their related history before moving the session.")

    moving_event_ids = {event["id"] for event in plan.events}
    incoming_events = [event for event in
                       store.relocation_workspace_events(source_id, limit=MAX_MOVE_SCAN)
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
    plan.incidences = history.incidences
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
        row = store.relocation_entity(eid)
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
        row = store.relocation_repo(rid)
        if row is None or row["workspace_id"] != source_id:
            raise ValueError("Related data has inconsistent project ownership.")
        repo = dict(row)
        target = store.relocation_repo_named(target_id, repo["name"])
        repo["target"] = dict(target) if target else None
        plan.repos.append(repo)
    repo_targets = {row["id"]: row["target"]["id"] if row["target"] else "new:" + row["id"]
                    for row in plan.repos}
    for entity in plan.entities:
        rid = repo_targets.get(entity["repo_id"])
        # Preserve distinct source aliases. Reusing every normalized-name match
        # would collapse their incidence rows and can violate the live uniqueness
        # constraints even though the original source graph was valid.
        target = store.relocation_entity_named(target_id, rid, entity["name"], entity["etype"])
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
            row = store.relocation_entity(current["canonical_id"])
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
            collision = store.relocation_canonical_entity(
                target_id, repo_targets.get(entity["repo_id"]),
                entity["normalized_name"], entity["etype"],
            )
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
        collision = store.relocation_edge_conflict(
            target_id, repo_targets.get(edge["repo_id"]), start, end, edge["relation"], edge["layer"],
        )
        if collision:
            target_conflicts.append(dict(collision))
            plan.block("target_graph_conflict", "The destination already has matching graph "
                       "relations. Use a whole-workspace merge to reconcile their evidence.")
    for record in plan.records:
        if (record.scope.value == "session" or not record.subject_key
                or record.valid_to is not None or record.expired_at is not None):
            continue
        collision = store.relocation_claim_conflict(target_id, repo_targets.get(record.repo_id), record)
        if collision:
            target_conflicts.append(dict(collision))
            plan.block("target_claim_conflict", "The destination already contains a claim with "
                       "the same key. Review that conflict before moving these memories.")
    ownership = store.relocation_ownership(source_id, target_id, limit=MAX_MOVE_SCAN)
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
    store.apply_memory_move(plan, actor=actor)
