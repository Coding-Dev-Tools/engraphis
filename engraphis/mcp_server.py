#!/usr/bin/env python3
"""Engraphis MCP server — give any MCP-capable agent persistent memory.

Exposes the Engraphis memory engine as Model Context Protocol tools so coding
agents (Claude Code, Cursor, Cline, Zed, Windsurf, …) and general agents can
``remember`` facts and ``recall`` them across sessions and repositories, scoped
to ``workspace → repo → session`` — plus the bi-temporal ``why``/``timeline``
tools, governance (``forget``/``pin``/``correct``), proactive recall, and
explicit linking/event logging.

Run it (stdio transport, the default for local MCP clients)::

    pip install "engraphis[mcp]"
    engraphis-mcp                      # or:  python -m engraphis.mcp_server

Register with Claude Code::

    claude mcp add engraphis -- engraphis-mcp

All tool logic and input validation live in :mod:`engraphis.service`; this module
is only the MCP binding, so the engine stays usable without the ``mcp`` package.
Tools use flat, top-level parameters so agents get a clean input schema.
"""
from __future__ import annotations

import json
from typing import Annotated, List, Optional

from pydantic import Field

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - exercised only without the optional dep
    raise SystemExit(
        "The 'mcp' package is required to run the Engraphis MCP server.\n"
        "Install it with:  pip install \"engraphis[mcp]\"   (or: pip install mcp)"
    )

from engraphis.config import settings
from engraphis.service import MemoryService, ValidationError

mcp = FastMCP("engraphis_mcp")

_service: Optional[MemoryService] = None


def set_service(svc: MemoryService) -> None:
    """Inject an external MemoryService (e.g. the dashboard's) so the MCP tools share
    ONE writer with the dashboard instead of opening a second connection to the same
    SQLite file (which would cause WAL ``database is locked`` contention — the exact
    problem ``scripts/mcp_server_http.py`` was written to avoid). When not injected,
    :func:`service` lazily builds a local service (standalone stdio/HTTP MCP)."""
    global _service
    _service = svc


def service() -> MemoryService:
    """Lazily build the service so server startup is instant (model loads on first use)."""
    global _service
    if _service is None:
        _service = MemoryService.create(
            settings.db_path,
            embed_model=settings.embed_model or None,
            allowed_workspaces=settings.allowed_workspaces,
            extractor=settings.extractor,
        )
    return _service


def _ok(payload: dict) -> str:
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def _err(exc: Exception) -> str:
    """Actionable, safe error string (never leaks internals)."""
    if isinstance(exc, ValidationError):
        return f"Error: {exc}"
    return f"Error: {type(exc).__name__}: {exc}"


@mcp.tool(
    name="engraphis_remember",
    annotations={"title": "Remember a fact", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def engraphis_remember(
    content: Annotated[str, Field(description="The fact, decision, convention, or note to "
                                  "store (e.g. 'We use pnpm for all frontend repos').",
                                  min_length=1, max_length=100_000)],
    workspace: Annotated[str, Field(description="Top-level scope, e.g. an org or product "
                                    "name ('acme'). Defaults to 'default' if omitted.",
                                    min_length=1, max_length=200)] = "default",
    repo: Annotated[Optional[str], Field(description="Repository scope within the workspace "
                                         "('backend'). Omit for workspace-wide memories.",
                                         max_length=200)] = None,
    session_id: Annotated[Optional[str], Field(description="Session id from "
                          "engraphis_start_session, if this memory belongs to one.")] = None,
    mtype: Annotated[str, Field(description="Memory type: 'semantic' (facts/conventions), "
                     "'episodic' (events/decisions), 'procedural' (how-tos), or "
                     "'working' (transient).")] = "semantic",
    scope: Annotated[str, Field(description="Visibility: 'session', 'repo', 'workspace', "
                     "or 'user'.")] = "repo",
    title: Annotated[str, Field(description="Optional short title.", max_length=1_000)] = "",
    importance: Annotated[float, Field(description="Salience 0..1; higher resists decay.",
                          ge=0.0, le=1.0)] = 0.0,
    keywords: Annotated[Optional[List[str]], Field(description="Optional keywords to aid "
                        "lexical recall.")] = None,
    dedupe: Annotated[bool, Field(description="If true (default), check this against similar "
                      "existing memories first: an exact restatement reinforces the existing "
                      "one instead of duplicating it, and a same-subject update supersedes the "
                      "old one (closed, not deleted) instead of leaving a contradiction. Set "
                      "false to force a plain insert (e.g. for recurring episodic log "
                      "entries where repeats are meaningful).")] = True,
    source: Annotated[str, Field(description="Provenance: who/what produced this memory — "
                      "e.g. 'agent:<role>', 'tool:<name>', 'human', or 'web'.",
                      max_length=200)] = "agent",
    trusted: Annotated[bool, Field(description="Set false for content originating from "
                       "untrusted input (web pages, third-party docs, tool output echoing "
                       "external text). Untrusted memories carry provenance.trusted=false "
                       "at recall so prompts can label them (memory-poisoning guard).")] = True,
    kind: Annotated[Optional[str], Field(description="Optional artifact kind for filtering: "
                    "'plan', 'diff', 'review', 'task_summary', 'council_verdict', ...",
                    max_length=100)] = None,
) -> str:
    """Store a memory so it can be recalled in later turns, sessions, or repos.

    Use this whenever you learn something worth keeping: a convention, a decision and its
    rationale, a bug's cause and fix, a user preference, or a reusable procedure.

    Returns:
        str: JSON ``{"id","workspace","repo","scope","mtype","stored":true,"op"}`` where
        ``op`` is ``"add"`` (new), ``"noop"`` (matched an existing memory almost exactly —
        that one was reinforced, ``id`` points to it), or ``"invalidate"`` (superseded an
        existing memory on the same subject — see ``superseded`` for the old id(s); history
        is preserved, never deleted). Returns ``"Error: <reason>"`` if validation fails.
    """
    try:
        return _ok(service().remember(
            content, workspace=workspace, repo=repo, session_id=session_id,
            mtype=mtype, scope=scope, title=title, importance=importance, keywords=keywords,
            source=source, trusted=trusted, kind=kind,
            resolve_conflicts=dedupe,
        ))
    except Exception as exc:  # noqa: BLE001 - surface a safe, actionable message
        return _err(exc)


@mcp.tool(
    name="engraphis_recall",
    annotations={"title": "Recall relevant memories", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_recall(
    query: Annotated[str, Field(description="What you want to remember, in natural language "
                                "(e.g. 'how do we handle auth?').", min_length=1,
                                max_length=100_000)],
    workspace: Annotated[Optional[str], Field(description="Restrict to this workspace.",
                                              max_length=200)] = None,
    repo: Annotated[Optional[str], Field(description="Restrict to this repo (requires "
                                         "workspace).", max_length=200)] = None,
    mtypes: Annotated[Optional[List[str]], Field(description="Restrict to these memory types "
                      "(semantic/episodic/procedural/working).")] = None,
    k: Annotated[int, Field(description="Max memories to return (1-50).", ge=1, le=50)] = 8,
) -> str:
    """Retrieve the memories most relevant to a query (hybrid vector + lexical + graph).

    Call this before answering or acting when prior context would help — to avoid re-asking
    the user, to recover decisions/conventions, or to resume earlier work.

    Returns:
        str: JSON with ``{"query","count","context","memories":[{"id","title","content",
        "scope","mtype","repo_id","score","arm","retention","provenance"}]}``. Returns
        count 0 with a "note" if the workspace/repo isn't known yet.
    """
    try:
        return _ok(service().recall(
            query, workspace=workspace, repo=repo, mtypes=mtypes, k=k,
        ))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_recall_grounded",
    annotations={"title": "Grounded recall (cited answer, or abstain)", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_recall_grounded(
    query: Annotated[str, Field(description="The question to answer from memory, in natural "
                                "language (e.g. 'which auth scheme did we standardise on?').",
                                min_length=1, max_length=100_000)],
    workspace: Annotated[Optional[str], Field(description="Restrict to this workspace.",
                                              max_length=200)] = None,
    repo: Annotated[Optional[str], Field(description="Restrict to this repo (requires "
                                         "workspace).", max_length=200)] = None,
    mtypes: Annotated[Optional[List[str]], Field(description="Restrict to these memory types "
                      "(semantic/episodic/procedural/working).")] = None,
    k: Annotated[int, Field(description="Max memories to consider (1-50).", ge=1, le=50)] = 8,
    min_support: Annotated[Optional[float], Field(description="Absolute support floor 0..1 "
                           "below which the tool abstains instead of answering. Omit for the "
                           "default; raise it to demand stronger evidence (0 disables the abstain gate).", ge=0.0,
                           le=1.0)] = None,
) -> str:
    """Answer a question *strictly from* stored memories, with citations — or abstain.

    Unlike ``engraphis_recall`` (which returns memories and leaves synthesis to you),
    this returns an answer assembled only from the retrieved memories, each claim tied
    to a ``[n]`` citation, and — crucially — refuses to answer when nothing in scope
    actually supports the query (``grounded: false``). Use it when you want a grounded,
    non-hallucinated answer and would rather get "insufficient evidence" than a guess.
    The answer is extractive/deterministic (no LLM is called), so it never introduces a
    claim that is not in a cited memory.

    Returns:
        str: JSON ``{"query","grounded","abstained","answer","support","reason",
        "synthesized":false,"citations":[{"n","id","title","content","score","support",
        "provenance"}]}``. When ``grounded`` is false, ``answer`` is empty and ``reason``
        explains why (insufficient evidence, or unknown workspace/repo).
    """
    try:
        return _ok(service().grounded_recall(
            query, workspace=workspace, repo=repo, mtypes=mtypes, k=k,
            min_support=min_support,
        ))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_why",
    annotations={"title": "Explain the rationale behind a fact", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_why(
    query: Annotated[str, Field(description="The decision or fact to explain, e.g. "
                                "'why did we migrate to PASETO?' or just 'rate limit'.",
                                min_length=1, max_length=100_000)],
    workspace: Annotated[str, Field(description="Workspace to search.", min_length=1,
                                    max_length=200)],
    repo: Annotated[Optional[str], Field(description="Restrict to this repo.",
                                         max_length=200)] = None,
    k: Annotated[int, Field(description="Max results (1-50).", ge=1, le=50)] = 5,
) -> str:
    """Surface the current answer *and* what it superseded, if anything.

    Use this for "why is it like this" / "what did we used to do" questions — it
    deliberately looks past the live view into bi-temporal history, which plain recall
    does not. The "supersedes" list is what makes this different from a vector search:
    those memories are no longer current but are not deleted, so the rationale chain
    ("we used to do X, then switched to Y because Z") stays answerable.

    Returns:
        str: JSON ``{"query","answer":[...live memories...],"supersedes":[...what they
        replaced, if anything...]}``. Raises an actionable error if the workspace/repo
        is unknown.
    """
    try:
        return _ok(service().why(query, workspace=workspace, repo=repo, k=k))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_timeline",
    annotations={"title": "Bi-temporal history of a fact", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_timeline(
    query: Annotated[str, Field(description="The fact/entity to trace, e.g. 'rate limit' or "
                                "'default branch name'.", min_length=1, max_length=100_000)],
    workspace: Annotated[str, Field(description="Workspace to search.", min_length=1,
                                    max_length=200)],
    repo: Annotated[Optional[str], Field(description="Restrict to this repo.",
                                         max_length=200)] = None,
    limit: Annotated[int, Field(description="Max history entries (1-50).", ge=1,
                     le=50)] = 20,
) -> str:
    """Return every version of a fact in chronological order, including superseded ones.

    Use this for "what did we believe and when" / "how has X changed over time" — each
    entry carries ``valid_from``/``valid_to`` so you can see exactly when it was true.

    Returns:
        str: JSON ``{"query","history":[{...memory fields..., "valid_from","valid_to"}]}``
        oldest first. Raises an actionable error if the workspace/repo is unknown.
    """
    try:
        return _ok(service().timeline(query, workspace=workspace, repo=repo, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_recall_proactive",
    annotations={"title": "What should I know right now", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_recall_proactive(
    workspace: Annotated[str, Field(description="Workspace to surface memories from.",
                                    min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo to surface memories from; also "
                                         "enables the last-session handoff.",
                                         max_length=200)] = None,
    k: Annotated[int, Field(description="Max memories to return (1-50).", ge=1, le=50)] = 10,
) -> str:
    """Conscious/proactive recall: high-importance, recent, well-reinforced memories with
    no query needed — call this at the start of a task to load context before you've
    figured out what to ask for. When ``repo`` is given, also returns the most recent
    *ended* session's summary and unresolved ``open_threads`` for that repo, so you can
    pick up exactly where the last session left off.

    Returns:
        str: JSON ``{"memories":[...], "last_session":{"summary","open_threads","outcome"}
        or {} if there is no prior session}``.
    """
    try:
        return _ok(service().recall_proactive(workspace=workspace, repo=repo, k=k))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_forget",
    annotations={"title": "Forget a memory", "readOnlyHint": False,
                 "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_forget(
    memory_id: Annotated[str, Field(description="The memory id to forget (from a prior "
                         "remember/recall result, e.g. 'mem_01J...').", min_length=1,
                         max_length=200)],
    workspace: Annotated[str, Field(description="Workspace that owns this memory — checked "
                                    "against the memory's actual workspace before anything is "
                                    "changed, so you can't forget a memory in a workspace you "
                                    "weren't already given.", min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo that owns this memory, if it's "
                                         "repo-scoped; also checked.",
                                         max_length=200)] = None,
    reason: Annotated[str, Field(description="Why this is being forgotten (recorded in the "
                      "audit trail).", max_length=1_000)] = "",
) -> str:
    """Retire a memory: it stops appearing in recall, but history is preserved, not
    deleted (bi-temporal close, never a hard delete) — use ``engraphis_correct`` instead
    if you have replacement content, since that keeps the "why" chain intact.

    Returns:
        str: JSON ``{"id","status":"forgotten","reason"}`` or an actionable error if the
        id is unknown or doesn't belong to ``workspace``/``repo``.
    """
    try:
        return _ok(service().forget(memory_id, workspace=workspace, repo=repo, reason=reason))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_pin",
    annotations={"title": "Pin or unpin a memory", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_pin(
    memory_id: Annotated[str, Field(description="The memory id to pin/unpin.", min_length=1,
                         max_length=200)],
    workspace: Annotated[str, Field(description="Workspace that owns this memory — checked "
                                    "against the memory's actual workspace before anything is "
                                    "changed.", min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo that owns this memory, if it's "
                                         "repo-scoped; also checked.",
                                         max_length=200)] = None,
    pinned: Annotated[bool, Field(description="True to pin (protect from future automatic "
                      "decay/pruning), false to unpin.")] = True,
) -> str:
    """Mark a memory as important enough to exempt from automatic decay/pruning — use for
    durable conventions or identity facts that must never silently fade.

    Returns:
        str: JSON ``{"id","pinned"}`` or an actionable error if the id is unknown or doesn't
        belong to ``workspace``/``repo``.
    """
    try:
        return _ok(service().pin(memory_id, workspace=workspace, repo=repo, pinned=pinned))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_correct",
    annotations={"title": "Correct a memory", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def engraphis_correct(
    memory_id: Annotated[str, Field(description="The memory id to correct.", min_length=1,
                         max_length=200)],
    new_content: Annotated[str, Field(description="The corrected content.", min_length=1,
                           max_length=100_000)],
    workspace: Annotated[str, Field(description="Workspace that owns this memory — checked "
                                    "against the memory's actual workspace before anything is "
                                    "changed.", min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo that owns this memory, if it's "
                                         "repo-scoped; also checked.",
                                         max_length=200)] = None,
    reason: Annotated[str, Field(description="Why this is being corrected (e.g. 'typo', "
                      "'the user clarified').", max_length=1_000)] = "",
) -> str:
    """Replace a memory's content without losing history: the old content is closed
    (bi-temporal invalidate, not deleted) and the correction is stored as a new memory
    that records what it corrects — so the audit trail and ``engraphis_why`` both still
    work afterward. Prefer this over forget+remember for fixes.

    Returns:
        str: JSON ``{"id","superseded":[old_id],"reason"}`` or an actionable error if the
        id is unknown or doesn't belong to ``workspace``/``repo``.
    """
    try:
        return _ok(service().correct(memory_id, new_content, workspace=workspace, repo=repo,
                                     reason=reason))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_link",
    annotations={"title": "Link two memories", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def engraphis_link(
    a: Annotated[str, Field(description="First memory id.", min_length=1, max_length=200)],
    b: Annotated[str, Field(description="Second memory id.", min_length=1, max_length=200)],
    workspace: Annotated[str, Field(description="Workspace that owns both memories — checked "
                                    "against each memory's actual workspace before linking.",
                                    min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo that owns both memories, if "
                                         "repo-scoped; also checked.",
                                         max_length=200)] = None,
    relation: Annotated[str, Field(description="Relationship label, e.g. 'related', "
                        "'caused_by', 'fixed_by'.", max_length=200)] = "related",
) -> str:
    """Explicitly connect two memories (A-MEM-style linking) — use when you notice two
    stored facts are related but a plain recall wouldn't surface that connection, e.g. a
    bug report and the memory describing its fix.

    Returns:
        str: JSON ``{"a","b","relation","linked":true}`` or an actionable error if either
        id is unknown or doesn't belong to ``workspace``/``repo``.
    """
    try:
        return _ok(service().link(a, b, workspace=workspace, repo=repo, relation=relation))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_record_event",
    annotations={"title": "Log an episodic event", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def engraphis_record_event(
    kind: Annotated[str, Field(description="Event kind, e.g. 'decision', 'bug', 'fix', "
                    "'tried_and_failed', 'review_comment'.", min_length=1, max_length=200)],
    content: Annotated[str, Field(description="What happened.", min_length=1,
                       max_length=100_000)],
    workspace: Annotated[str, Field(description="Workspace this event belongs to. "
                                    "Defaults to 'default' if omitted.",
                                    min_length=1, max_length=200)] = "default",
    repo: Annotated[Optional[str], Field(description="Repo this event belongs to.",
                                         max_length=200)] = None,
    session_id: Annotated[Optional[str], Field(description="Session this event belongs to, "
                          "if any.")] = None,
) -> str:
    """Append a lightweight episodic log entry — lower ceremony than ``engraphis_remember``,
    for raw events you may later want consolidated into a durable fact (e.g. "tried X, it
    deadlocked" — three of these about the same thing is a signal worth promoting).

    Returns:
        str: JSON ``{"id","kind"}``.
    """
    try:
        return _ok(service().record_event(kind, content, workspace=workspace, repo=repo,
                                          session_id=session_id))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_index_repo",
    annotations={"title": "Index a repository's code graph", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_index_repo(
    workspace: Annotated[str, Field(description="Workspace the repo belongs to.",
                                    min_length=1, max_length=200)],
    repo: Annotated[str, Field(description="Repo name to index.", min_length=1,
                               max_length=200)],
    root_path: Annotated[str, Field(description="Local filesystem path to the repo root "
                         "to parse (e.g. '/home/user/projects/myrepo'). Reads files from "
                         "this path the same way any local tool you have would.",
                         min_length=1, max_length=4_000)],
    languages: Annotated[Optional[List[str]], Field(description="Restrict to these "
                         "languages (e.g. ['python','csharp']). Names are normalised "
                         "('C#'->csharp, 'cpp'/'c++'->cpp). An unsupported name returns an "
                         "error listing what's supported, instead of silently indexing "
                         "nothing. Omit to index every supported language found.")] = None,
) -> str:
    """Parse a repository into the code symbol graph: function/class/method definitions
    plus best-effort calls/imports edges. Run this once when you start working in a repo
    (or after large changes) so ``engraphis_search_code`` has something to search — uses
    AST parsing (tree-sitter) when available, a dependency-free regex fallback otherwise.
    Supported languages: Python, JavaScript, TypeScript, C#, C, and C++.

    Build/dependency directories (node_modules, bin, obj, target, .venv, …) are skipped
    while walking, so a large non-Python repo indexes quickly instead of appearing to
    hang; add a ``.engraphisignore`` file (gitignore-style) at the repo root to skip
    project-specific generated files.

    Creates the workspace/repo if you haven't named them before (like
    engraphis_remember). Re-indexing is safe to call again; each file's symbols are
    replaced, not duplicated. Reads files from ``root_path`` on the local filesystem —
    the same trust boundary as any other local tool you have, nothing is sent anywhere.

    Returns:
        str: JSON ``{"files_indexed","symbols","edges","backend"}``.
    """
    try:
        return _ok(service().index_repo(workspace=workspace, repo=repo, root_path=root_path,
                                        languages=languages))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_search_code",
    annotations={"title": "Search the code symbol graph", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_search_code(
    query: Annotated[str, Field(description="A symbol name or partial name to find, e.g. "
                                "'Calculator' or 'add'.", min_length=1, max_length=500)],
    workspace: Annotated[str, Field(description="Workspace the repo belongs to.",
                                    min_length=1, max_length=200)],
    repo: Annotated[str, Field(description="Repo to search (must have been indexed with "
                               "engraphis_index_repo first).", min_length=1,
                               max_length=200)],
    limit: Annotated[int, Field(description="Max symbols to return (1-50).", ge=1,
                     le=50)] = 20,
) -> str:
    """Find function/class/method definitions by name, with their callers — structural
    code search that costs far fewer tokens than grepping/reading whole files, and
    directly answers "what calls this" / "what might break if I change it".

    Returns:
        str: JSON ``{"query","symbols":[{"name","fqname","kind","file","span",
        "signature","called_by":[{"src","file","line"}]}]}``.
    """
    try:
        return _ok(service().search_code(query, workspace=workspace, repo=repo, limit=limit))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_start_session",
    annotations={"title": "Start a memory session", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_start_session(
    workspace: Annotated[str, Field(description="Workspace the session belongs to.",
                                    min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repo scope, if any.",
                                         max_length=200)] = None,
    agent: Annotated[str, Field(description="Agent/tool name (e.g. 'claude-code').",
                                max_length=200)] = "",
    goal: Annotated[str, Field(description="What this session is trying to accomplish.",
                               max_length=1_000)] = "",
    force_new: Annotated[bool, Field(description="Force a brand-new session even if one is "
                         "already active for this workspace/repo/agent. Default false: a "
                         "repeat call in the same scope returns the existing active session "
                         "(reused=true) rather than opening a second one. Set true only for "
                         "a genuinely separate task in the same repo.")] = False,
) -> str:
    """Open a session to group this work's memories and enable cross-session resume.

    Call this at the start of a task in a repo you've worked in before — if a previous
    session in that repo was ended with a summary or open threads, they come back in
    ``bootstrap`` so you can pick up where it left off instead of starting cold.

    Idempotent: calling it again in the same ``(workspace, repo, agent)`` scope returns
    the session already in progress (``reused: true``) instead of forking a second
    concurrent session — two live sessions on one scope means two writers contending on
    the store. Use ``force_new=true`` when you really do want a separate session.

    Returns:
        str: JSON ``{"session_id","workspace","repo","goal","status":"active","reused",
        "bootstrap":{"summary","open_threads","outcome"} or {} if there is no prior
        session}``. Pass ``session_id`` to engraphis_remember and engraphis_end_session.
    """
    try:
        return _ok(service().start_session(workspace, repo=repo, agent=agent, goal=goal,
                                           force_new=force_new))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_end_session",
    annotations={"title": "End a memory session", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_end_session(
    session_id: Annotated[str, Field(description="Session id from engraphis_start_session.",
                                     min_length=1, max_length=200)],
    summary: Annotated[str, Field(description="Summary of what happened, stored for resume.",
                                  max_length=100_000)] = "",
    outcome: Annotated[str, Field(description="Short outcome label (e.g. 'shipped', "
                                  "'blocked').", max_length=1_000)] = "",
    open_threads: Annotated[Optional[List[str]], Field(description="Unresolved items to "
                            "carry into the next session in this repo (e.g. 'tests 3-5 "
                            "still failing'). Surfaced automatically when that next "
                            "session starts.")] = None,
) -> str:
    """Close a session with a summary/outcome so the next session can pick up the thread.

    Returns:
        str: JSON ``{"session_id","status":"summarized","summary","open_threads"}`` or
        ``"Error: ..."`` if the session id is unknown.
    """
    try:
        return _ok(service().end_session(session_id, summary=summary, outcome=outcome,
                                         open_threads=open_threads))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_stats",
    annotations={"title": "Memory store stats", "readOnlyHint": True,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_stats(
    workspace: Annotated[Optional[str], Field(description="Limit counts to this workspace.",
                                              max_length=200)] = None,
) -> str:
    """Report memory counts (overall or for one workspace) — handy for onboarding/health.

    Returns:
        str: JSON ``{"memories","by_type","workspaces","sessions","schema_version"}``.
    """
    try:
        return _ok(service().stats(workspace=workspace))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_ingest",
    annotations={"title": "Ingest raw text (extract facts first)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
)
def engraphis_ingest(
    content: Annotated[str, Field(description="Raw, undistilled text: a conversation "
                                  "excerpt, meeting notes, a log, a long update. Engraphis "
                                  "extracts the discrete facts worth keeping (when an "
                                  "extractor is configured via ENGRAPHIS_EXTRACTOR=llm) "
                                  "and stores each one; otherwise stores the text as one "
                                  "memory.", min_length=1, max_length=100_000)],
    workspace: Annotated[str, Field(description="Top-level scope, e.g. an org or product "
                                    "name ('acme').", min_length=1, max_length=200)],
    repo: Annotated[Optional[str], Field(description="Repository scope within the "
                                         "workspace.", max_length=200)] = None,
    session_id: Annotated[Optional[str], Field(description="Session id from "
                          "engraphis_start_session, if any.")] = None,
    mtype: Annotated[str, Field(description="Default memory type for facts the extractor "
                     "doesn't classify: semantic/episodic/procedural/working.")] = "semantic",
    scope: Annotated[str, Field(description="Visibility: 'session', 'repo', 'workspace', "
                     "or 'user'.")] = "repo",
) -> str:
    """Store raw text without hand-distilling it first — the extract-then-remember path.

    Prefer ``engraphis_remember`` when you already have a crisp fact; use this when you
    have a blob (transcript, notes, long status update) and want Engraphis to break it
    into separate, individually-recallable memories. Each extracted fact goes through
    the same conflict resolution and evolution as a normal remember.

    Returns:
        str: JSON ``{"workspace","repo","count","extracted","facts":[{"id","op",...}]}``
        where ``extracted`` is false when no extractor is configured (passthrough).
    """
    try:
        return _ok(service().ingest(
            content, workspace=workspace, repo=repo, session_id=session_id,
            mtype=mtype, scope=scope,
        ))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_consolidate",
    annotations={"title": "Consolidate memories (sleep-time sweep)", "readOnlyHint": False,
                 "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
)
def engraphis_consolidate(
    workspace: Annotated[str, Field(description="Workspace to consolidate.", min_length=1,
                                    max_length=200)],
    repo: Annotated[Optional[str], Field(description="Restrict to this repo.",
                                         max_length=200)] = None,
    dry_run: Annotated[bool, Field(description="If true (default), only report what would "
                       "happen — recommended before the first real run.")] = True,
    profiles: Annotated[bool, Field(description="Also roll each entity's scattered "
                        "memories into one durable profile digest (needs graph "
                        "entities). Report lands under 'profiles'.")] = False,
) -> str:
    """Run one sleep-time consolidation sweep: recurring episodic memories on the same
    subject are distilled into one durable semantic digest (linked to its sources), and
    fully-decayed transient memories are archived (bi-temporally closed — never deleted,
    always audited, pinned memories exempt). Idempotent: already-consolidated clusters
    are skipped. With ``profiles=True`` each entity's memories are also rolled into one
    durable profile digest. Good moments to call it: session end, or on a schedule.

    Returns:
        str: JSON report ``{"clusters_found","digests_created","archived",
        "skipped_already_consolidated","compaction","dry_run"}`` — ``compaction`` reports
        the context tokens the sweep saved. With ``profiles=True`` a ``profiles`` block is
        added (``entities_considered``, ``profiles_created``, ``compaction``).
    """
    try:
        return _ok(service().consolidate(workspace=workspace, repo=repo, dry_run=dry_run,
                                         profiles=profiles))
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


@mcp.tool(
    name="engraphis_answer",
    annotations={"title": "Grounded answer (grounded recall + synthesis)",
                 "readOnlyHint": True, "openWorldHint": False},
)
def engraphis_answer(
    query: Annotated[str, Field(description="The question to answer from memory. Natural language, e.g. 'how do we handle auth?'.",
                                  min_length=1, max_length=10_000)],
    workspace: Annotated[str, Field(description="Top-level scope, e.g. an org or product name ('acme'). Defaults to 'default' if omitted.",
                                    min_length=1, max_length=200)] = "default",
    repo: Annotated[Optional[str], Field(description="Repository scope within the workspace.",
                                         max_length=200)] = None,
    k: Annotated[int, Field(description="Max memories to consider (1-50).", ge=1, le=50)] = 8,
    min_support: Annotated[float, Field(description="Absolute support floor 0..1. Memories below this don't count as evidence. Default 0.25.", ge=0.0, le=1.0)] = 0.25,
    synthesize: Annotated[bool, Field(description="If true, ask LLM to write prose answer with citations; if false (default), return extractive answer with citations.")] = False,
) -> str:
    """Grounded answer from memory — not just memories, but an *answer*.

    Runs grounded recall (hybrid vector + lexical + graph + rerank) and returns either:
    * An extractive answer (citations only, deterministic, offline) — always safe.
    * A synthesised prose answer with inline [n] citations — if ``synthesize=True" and an LLM is configured.

    If evidence is below the support floor, returns ``grounded=false, abstained=true" with a reason — never hallucinates.
    Every claim is cited with [n] linking to the source memory. The deterministic path never introduces claims not in the sources.
    """
    try:
        result = service().grounded_recall(query=query, workspace=workspace, repo=repo, k=k,
                                           min_support=min_support)
        return _ok({
            "query": result.get("query", query),
            "answer": result.get("answer", ""),
            "grounded": result.get("grounded", False),
            "abstained": result.get("abstained", True),
            "reason": result.get("reason", ""),
            "support": result.get("support", 0.0),
            "synthesized": False,
            "citations": result.get("citations", []),
        })
    except Exception as exc:  # noqa: BLE001
        return _err(exc)


def main() -> None:
    """Console entry point (``engraphis-mcp``). Runs over stdio."""
    mcp.run()
