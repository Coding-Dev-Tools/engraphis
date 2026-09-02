"""EngraphisPrimeAgent (single sub-agent) and PrimeAgentFleet (8 sub-agents)."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Iterable

from .config import (
    DEFAULT_AGENT_NAMES,
    EngraphisRuntimeConfig,
    build_runtime_config,
)
from .mcp_client import EngraphisMcpClient, EngraphisMcpToolError
from .tools import ToolFn, all_tools, build_tool, TOOL_SPECS, validate_args

_logger = logging.getLogger("engraphis_prime_agent.agent")


class _UnsetRepo:
    """Sentinel used to distinguish an omitted repo from an explicit null."""


_UNSET_REPO = _UnsetRepo()


class EngraphisPrimeAgent:
    """One named sub-agent owning its own Engraphis session.

    Holds:
      - a shared EngraphisMcpClient (one stdio subprocess for the whole fleet)
      - a per-agent session id (started lazily on first tool call)
      - the 9 Smart tools as (callable, schema) pairs
    """

    def __init__(
        self,
        name: str,
        client: EngraphisMcpClient,
        config: EngraphisRuntimeConfig,
        *,
        workspace: str | None = None,
        repo: str | None = None,
        goal: str = "",
        token_budget: int = 512,
    ) -> None:
        if not name or not name.strip():
            raise ValueError("Sub-agent name must be non-empty.")
        self.name = name.strip()
        self.client = client
        self.config = config
        # Workspace precedence: explicit per-agent kwarg > config default.
        # > the literal "default" placeholder so the Smart server always
        # sees an explicit workspace (the "default" workspace is the
        # server's own well-known scope for the Smart MCP gateway).
        self.workspace = workspace or config.default_workspace or "default"
        # Repo precedence: explicit per-agent kwarg > config default > sub-agent
        # name. A single effective repo must be used for both session creation
        # and the tool-call defaults — a session opened in `researcher` while
        # tools send `api` is rejected by MemoryService with "session_id does
        # not belong to that workspace/repo". When ENGRAPHIS_REPO sets a
        # fleet-wide default, every sub-agent's session and every tool call
        # use that same repo; only when no default is configured does the
        # sub-agent name double as the repo, giving per-role isolation by
        # default.
        if repo is not None:
            self.repo = repo
        elif config.default_repo is not None:
            self.repo = config.default_repo
        else:
            self.repo = self.name
        self._session_agent = self.name
        self.goal = goal
        self.token_budget = token_budget
        self._session_id: str | None = None
        # Raw server response from the most recent successful
        # ``engraphis_session(start)`` call. Returned (rather than a
        # second recall) when an external caller invokes the
        # lifecycle tool via ``agent.call("engraphis_session", ...)``
        # so the bounded context, sources, usage, and
        # ``context_status`` survive the round-trip. Reset to None
        # at the end of every session.
        self._last_session_response: dict[str, Any] | None = None
        self._session_lock = asyncio.Lock()
        self._tools: dict[str, tuple[ToolFn, dict[str, Any]]] | None = None
        self._closed = False
        self._closing = False
        # Protects lazy initialization of the tool-binding cache. The
        # session lock above is *not* enough because get_tool() and tools()
        # are synchronous and can be called from multiple threads (or, in
        # the future, multiple event-loop iterations) on a fresh agent
        # before start_session() has run. threading.Lock is correct here:
        # the method is sync, and we just need mutual exclusion across
        # concurrent sync callers — not coordination with awaits.
        self._tools_lock = threading.Lock()

    def __repr__(self) -> str:
        sid = self._session_id if self._session_id else "none"
        return (
            f"EngraphisPrimeAgent(name={self.name!r}, workspace={self.workspace!r}, "
            f"repo={self.repo!r}, session_id={sid!r})"
        )

    # --- session lifecycle ------------------------------------------------

    async def start_session(
        self,
        *,
        force_new: bool = False,
        workspace: str | None = None,
        repo: str | None | _UnsetRepo = _UNSET_REPO,
        agent: str | None = None,
        goal: str | None = None,
        token_budget: int | None = None,
    ) -> str:
        # The two state mutations below happen under _session_lock so they
        # are atomic w.r.t. concurrent start_session / end_session callers
        # (and concurrent get_tool() callers that read self._session_id).
        async with self._session_lock:
            self._ensure_open()
            requested_workspace = self.workspace if workspace is None else workspace
            requested_repo = self.repo if isinstance(repo, _UnsetRepo) else repo
            requested_agent = self._session_agent if agent is None else agent
            requested_goal = self.goal if goal is None else goal
            requested_budget = self.token_budget if token_budget is None else token_budget
            has_overrides = any(
                value is not None
                for value in (workspace, agent, goal, token_budget)
            ) or not isinstance(repo, _UnsetRepo)
            request_force_new = force_new or (
                self._session_id is not None
                and goal is not None
                and goal != self.goal
            )
            if self._session_id and not force_new and not has_overrides:
                return self._session_id
            args: dict[str, Any] = {
                "action": "start",
                "agent": requested_agent,
                "force_new": request_force_new,
                "goal": requested_goal,
                "token_budget": requested_budget,
            }
            if requested_workspace is not None:
                args["workspace"] = requested_workspace
            if requested_repo is not None:
                args["repo"] = requested_repo
            response = await self.client.call_tool("engraphis_session", args)
            session_id = self._extract_session_id(response)
            if not session_id:
                raise EngraphisMcpToolError(
                    f"engraphis_session(start) for agent={self.name!r} returned no session_id."
                )
            # Atomic state transition: only one writer holds this lock.
            self._session_id = session_id
            self._tools = None  # rebuild bindings with the new session id
            # Remember the raw server response so the explicit
            # ``engraphis_session`` callback can return the bounded
            # recalled context, sources, usage, and ``context_status``
            # the Smart server computed for this goal. Without this,
            # ``agent.call("engraphis_session", {action: "start",
            # goal: "..."})`` would perform a second recall (against
            # the now-cached session) and double the latency.
            self._last_session_response = response
            self._session_agent = requested_agent
            self.workspace = requested_workspace
            self.repo = requested_repo
            self.goal = requested_goal
            self.token_budget = requested_budget
            return session_id

    async def end_session(
        self,
        *,
        summary: str = "",
        outcome: str = "",
        open_threads: list[str] | None = None,
        session_id: str | None = None,
        agent: str | None = None,
        workspace: str | None = None,
        repo: str | None = None,
    ) -> None:
        # Hold the lock through the close RPC so a concurrent start_session
        # cannot create a replacement session while the old one is still
        # being closed.
        async with self._session_lock:
            active_session_id = self._session_id
            target_session_id = session_id or active_session_id
            if not target_session_id:
                return
            # Always clear local state, even if the gateway call fails, so
            # the sub-agent is not stuck in a half-open state.
            if target_session_id == active_session_id:
                self._session_id = None
                self._last_session_response = None
                self._tools = None
            end_args: dict[str, Any] = {
                "action": "end",
                "agent": self._session_agent if agent is None else agent,
                "session_id": target_session_id,
                "summary": summary,
                "outcome": outcome,
            }
            if open_threads is not None:
                # ``open_threads`` is the server's next-session handoff. The
                # MCP schema treats this field as nullable; we forward the
                # list as-is so an empty list clears prior follow-ups and a
                # non-empty list replaces them. Omitting the key entirely
                # leaves the server's prior threads untouched.
                end_args["open_threads"] = open_threads
            if workspace is not None:
                end_args["workspace"] = workspace
            if repo is not None:
                end_args["repo"] = repo
            # Re-raise the gateway error after clearing the cached id. The
            # lifecycle dispatcher catches this and converts it into a
            # structured "close_failed" response; direct callers see the
            # same error shape.
            await self.client.call_tool("engraphis_session", end_args)

    @property
    def session_id(self) -> str | None:
        return self._session_id

    # --- tool access ------------------------------------------------------

    def _ensure_tools(self) -> dict[str, tuple[ToolFn, dict[str, Any]]]:
        # Fast path: bindings already built. The lock is only for the slow
        # path so we don't pay synchronization cost on every tool access.
        if self._tools is not None:
            return self._tools
        # Two coroutines that race here on a fresh agent must not both
        # build (and leak) duplicate bindings. asyncio.Lock is fair, so
        # the second waiter will see self._tools already populated.
        # Note: a synchronous lock is fine because this method is sync;
        # we just need mutual exclusion against other sync call sites.
        #
        # Build tools with the agent's effective scope. An agent created
        # with explicit ``workspace=`` / ``repo=`` overrides keeps those
        # values for both the session and every tool call; without this
        # the apply_scope_defaults path would inject config.default_*
        # alongside the explicit values, which MemoryService rejects.
        effective_config = self._effective_config()
        with self._tools_lock:
            if self._tools is None:
                self._tools = {
                    meta["name"]: build_tool(
                        meta["name"],
                        self.client,
                        effective_config,
                        session_id=self._session_id,
                    )
                    for _fn, meta in all_tools(
                        self.client, effective_config, session_id=self._session_id
                    )
                }
        return self._tools

    def _effective_config(self) -> EngraphisRuntimeConfig:
        """A copy of ``self.config`` with the agent's effective workspace/repo.

        ``apply_scope_defaults`` reads workspace/repo defaults from the
        passed-in config, so an agent that overrides these scopes must
        build a config whose defaults match the override. Otherwise
        MemoryService rejects the call with "session_id does not belong
        to that workspace/repo".
        """
        if (
            self.workspace == self.config.default_workspace
            and self.repo == self.config.default_repo
        ):
            return self.config
        return EngraphisRuntimeConfig(
            command=self.config.command,
            args=self.config.args,
            cwd=self.config.cwd,
            default_workspace=self.workspace,
            default_repo=self.repo,
            environment=dict(self.config.environment),
        )

    def tools(self) -> list[tuple[ToolFn, dict[str, Any]]]:
        bindings = self._ensure_tools()
        return [bindings[name] for name, _schema in TOOL_SPECS]

    def get_tool(self, name: str) -> tuple[ToolFn, dict[str, Any]]:
        return self._ensure_tools()[name]

    async def _call_data_tool(
        self, tool: str, args: dict[str, Any], ctx: Any = None
    ) -> dict[str, Any]:
        """Run a data tool against one stable session generation.

        Session lifecycle calls hold ``_session_lock`` through their gateway
        RPC. Data calls must use the same lock through binding lookup and the
        RPC, otherwise a concurrent force-new start can replace the cached
        session after the binding was captured but before the request is sent.
        """
        while True:
            if not self._session_id:
                await self.start_session()
            async with self._session_lock:
                self._ensure_open()
                # An end may have acquired the lock between the lazy-start
                # check and this block. Retry so the next request cannot be
                # sent without a live session id.
                if not self._session_id:
                    continue
                fresh_fn, _schema = self.get_tool(tool)
                return await fresh_fn(args, ctx)

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self._ensure_open()
        # Lifecycle calls must route through the agent's own state
        # machine so the cached ``_session_id`` stays in sync with the
        # server session; an "end" would otherwise leave the agent
        # holding a closed id, and a "start" with force_new would
        # create a new server session whose id is not cached. This
        # mirrors the registration wrapper's special case.
        if tool == "engraphis_session":
            return await self._dispatch_session_lifecycle(args)
        return await self._call_data_tool(tool, args)

    # --- registration into prime-agent -----------------------------------

    def register(self, target: Any) -> Any:
        """Register all 9 tools into a prime-agent Agent (or compatible).

        The assumed contract is ``target.register_tool(name, fn, schema=...)``
        (LangChain/CrewAI-style). If prime-agent's actual API differs, this
        is the single function the implementer needs to adjust.

        The framework may invoke the registered callables directly rather
        than going through ``EngraphisPrimeAgent.call()``, so each registered
        tool is wrapped to lazily start the session on first invocation.
        Without this wrapper, the advertised registration path would never
        create or inject a per-agent session, and MemoryService would reject
        every call.
        """
        # Validate both presence and that it's actually a method (hasattr
        # would otherwise accept an attribute that happens to be a string
        # or a class-level descriptor that isn't callable).
        register_tool = getattr(target, "register_tool", None)
        if not callable(register_tool):
            raise TypeError(
                f"Cannot register tools on {type(target).__name__}: "
                "expected a callable `register_tool` method. "
                "See agent.py for the adapter point."
            )
        for fn, meta in self.tools():
            register_tool(meta["name"], self._wrap_for_registration(fn, meta["name"]),
                          schema=meta)
        return target

    def _wrap_for_registration(
        self, bound_fn: ToolFn, tool_name: str
    ) -> ToolFn:
        """Return a callable that lazily starts a session, then delegates.

        Mirrors the lazy-start behaviour of ``EngraphisPrimeAgent.call()`` so
        that frameworks which invoke the registered tool directly (bypassing
        ``call()``) still get a per-agent session injected. Re-fetches the
        current binding on every invocation so a session-id refresh in
        ``start_session`` (which invalidates the cached tool map) is
        honoured on the next call, not only the first one.

        The ``engraphis_session`` tool is special-cased to route through
        ``start_session``/``end_session`` so a framework-driven
        ``action: "start", force_new: true`` updates the cached
        ``_session_id``, and an explicit ``action: "end"`` clears it.
        Without this routing the wrapper would treat the lifecycle
        call like any other data tool and leave ``_session_id`` pointing
        to a session the server has already closed.
        """
        agent = self

        async def _wrapper(args: dict[str, Any], ctx: Any = None) -> dict[str, Any]:
            agent._ensure_open()
            if tool_name == "engraphis_session":
                return await agent._dispatch_session_lifecycle(args)
            return await agent._call_data_tool(tool_name, args, ctx)

        return _wrapper

    async def _dispatch_session_lifecycle(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        """Route a framework-driven engraphis_session call through the
        proper lifecycle methods so ``_session_id`` stays in sync with
        the server's session state.
        """
        # Lifecycle calls bypass ``build_tool`` because they must update the
        # agent's cached session state. Validate them at this boundary so a
        # misspelled or unsupported field cannot be silently dropped while
        # the hand-written routing below forwards only known arguments.
        args = validate_args("engraphis_session", args)
        action = args.get("action", "start")
        action = {
            "start_session": "start",
            "end_session": "end",
        }.get(action, action)
        if action not in {"start", "end"}:
            raise EngraphisMcpToolError(
                "engraphis_session action must be 'start' or 'end'."
            )
        if action == "end":
            end_kwargs: dict[str, Any] = {
                "summary": args.get("summary", ""),
                "outcome": args.get("outcome", ""),
            }
            for key in ("open_threads", "session_id", "agent", "workspace", "repo"):
                if key in args:
                    end_kwargs[key] = args[key]
            # ``open_threads`` is the server's next-session handoff;
            # dropping it would silently strip the caller-advertised
            # follow-ups, so always forward it through ``end_session``.
            try:
                await self.end_session(**end_kwargs)
                return {"status": "closed"}
            except EngraphisMcpToolError as exc:
                return {
                    "status": "close_failed",
                    "error": str(exc),
                }
        # Default to start. Forward every start argument advertised by the
        # Smart schema. ``start_session`` updates the cached scope/goal and
        # tool bindings only after the gateway returns a session id.
        force_new = args.get("force_new", False)
        if not isinstance(force_new, bool):
            raise EngraphisMcpToolError(
                "engraphis_session force_new must be a boolean."
            )
        start_kwargs: dict[str, Any] = {"force_new": force_new}
        for key in ("workspace", "repo", "agent", "goal", "token_budget"):
            if key in args:
                start_kwargs[key] = args[key]
        await self.start_session(**start_kwargs)
        # Rebuild tools with the new session id before returning so the
        # caller's next tool invocation does not see the stale binding.
        self._tools = None
        # Prefer the raw server response (carrying bounded recalled
        # context, sources, usage, and ``context_status`` when the
        # caller supplied a ``goal``) over a synthetic envelope. The
        # synthetic envelope would force a second recall against the
        # just-cached session and double the latency for callers
        # that already have a session id in hand.
        if self._last_session_response is not None:
            response = dict(self._last_session_response)
            response.setdefault("session_id", self._session_id)
            response.setdefault("action", "start")
            response.setdefault("agent", self.name)
            return response
        return {
            "session_id": self._session_id,
            "action": "start",
            "agent": self.name,
        }

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "repo": self.repo,
            "goal": self.goal,
            "session_id": self._session_id,
            "tools_bound": self._tools is not None,
        }

    def _ensure_open(self) -> None:
        if self._closed or self._closing:
            raise RuntimeError("EngraphisPrimeAgent is closed")

    # --- helpers ----------------------------------------------------------

    @staticmethod
    def _extract_session_id(response: dict[str, Any]) -> str | None:
        for block in response.get("content", []) or []:
            text = block.get("text")
            if not isinstance(text, str):
                continue
            try:
                parsed = json.loads(text)
            except (ValueError, TypeError):
                continue
            if isinstance(parsed, dict):
                sid = parsed.get("session_id") or parsed.get("sessionId")
                if isinstance(sid, str) and sid:
                    return sid
        return None


class PrimeAgentFleet:
    """N named sub-agents sharing one Engraphis stdio gateway.

    Use as an async context manager so the subprocess is shut down cleanly::

        async with PrimeAgentFleet(workspace="myrepo") as fleet:
            await fleet["researcher"].call("engraphis_recall_context", {"query": "..."})
    """

    def __init__(
        self,
        *,
        workspace: str | None = None,
        repo: str | None = None,
        agent_names: Iterable[str] | None = None,
        config: EngraphisRuntimeConfig | None = None,
        goals: dict[str, str] | None = None,
    ) -> None:
        base = config or build_runtime_config()
        if workspace or repo is not None:
            base = EngraphisRuntimeConfig(
                command=base.command,
                args=base.args,
                cwd=base.cwd,
                default_workspace=workspace if workspace is not None else base.default_workspace,
                default_repo=repo if repo is not None else base.default_repo,
                environment=dict(base.environment),
            )
        self.config = base
        self._client = EngraphisMcpClient(self.config)
        names = tuple(agent_names) if agent_names else DEFAULT_AGENT_NAMES
        self._goals = goals or {}
        self._agents: dict[str, EngraphisPrimeAgent] = {
            n: EngraphisPrimeAgent(
                n,
                self._client,
                self.config,
                workspace=workspace,
                repo=repo,
                goal=self._goals.get(n, ""),
            )
            for n in names
        }
        self._stack: AsyncExitStack | None = None
        self._closed = False
        self._closing = False

    # --- collection protocol ---------------------------------------------

    def __getitem__(self, name: str) -> EngraphisPrimeAgent:
        """Look up a sub-agent by name. Raises KeyError for unknown names.

        Example::

            agent = fleet["researcher"]
        """
        return self._agents[name]

    def __iter__(self):
        """Iterate over sub-agents in insertion order (matches `names()`)."""
        return iter(self._agents.values())

    def __len__(self) -> int:
        """Return the number of sub-agents in the fleet (default 8)."""
        return len(self._agents)

    def __contains__(self, name: object) -> bool:
        """Return True if a sub-agent with the given name is in the fleet.

        Example::

            if "researcher" in fleet:
                ...
        """
        return name in self._agents

    def names(self) -> tuple[str, ...]:
        """Return the sub-agent names in insertion order."""
        return tuple(self._agents)

    def status(self) -> dict[str, Any]:
        return {
            "workspace": self.config.default_workspace,
            "agents": [a.status() for a in self._agents.values()],
            "clientGeneration": self._client.generation(),
        }

    @property
    def client(self) -> EngraphisMcpClient:
        return self._client

    # --- lifecycle --------------------------------------------------------

    async def __aenter__(self) -> "PrimeAgentFleet":
        if self._closed or self._closing:
            raise RuntimeError("PrimeAgentFleet is closed")
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(self._client)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._closed or self._closing:
            return
        self._closing = True
        for agent in self._agents.values():
            agent._closing = True
        try:
            # Best-effort: end every active session, then close the stdio gateway.
            # The closing flag blocks new calls while these end RPCs are in flight.
            await asyncio.gather(
                *(a.end_session() for a in self._agents.values()),
                return_exceptions=True,
            )
            if self._stack is not None:
                await self._stack.aclose()
                self._stack = None
            else:
                await self._client.close()
        finally:
            for agent in self._agents.values():
                agent._closing = False
                agent._closed = True
            self._closing = False
            self._closed = True

    async def aclose(self) -> None:
        # Use the same guarded path for bare fleets and async context-managed
        # fleets. A bare fleet has no exit stack, so __aexit__ closes the
        # client directly after ending the active sessions.
        await self.__aexit__(None, None, None)

    # --- fan-out helpers -------------------------------------------------

    async def start_all_sessions(
        self,
    ) -> dict[str, Any]:
        """Warm up the fleet by starting every sub-agent's session eagerly.

        prime-agent schedulers that require the first tool call to never
        block on session bootstrap should call this once before dispatching.

        Returns a dict that always carries these two keys (so callers can
        rely on the shape regardless of partial failures):

          - ``"sessions"``: ``dict[str, str]`` mapping sub-agent name to
            session id for every sub-agent whose start succeeded.
          - ``"errors"``:   ``dict[str, BaseException]`` mapping sub-agent
            name to the exception raised for every sub-agent whose start
            failed. Empty if everything succeeded.

        Using ``asyncio.gather(..., return_exceptions=True)`` ensures a
        single failing sub-agent does not abort the warm-up for the
        others, and the structured ``errors`` dict makes partial failures
        observable (previously they were only logged).
        """
        coros: list[Awaitable[str]] = [
            agent.start_session() for agent in self._agents.values()
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        sessions: dict[str, str] = {}
        errors: dict[str, BaseException] = {}
        for name, value in zip(self._agents, results):
            if isinstance(value, BaseException):
                _logger.warning("start_session for %s failed: %s", name, value)
                errors[name] = value
                continue
            if isinstance(value, str) and value:
                sessions[name] = value
        return {"sessions": sessions, "errors": errors}

    async def fan_out(
        self,
        tool: str,
        per_agent_args: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Run the same tool across multiple sub-agents concurrently.

        Each sub-agent awaits its own session start (which serializes on the
        stdio transport through _call_lock). Framework-level concurrency is
        preserved because asyncio.gather issues the calls as separate coroutines.

        Args:
            tool: The MCP tool name to invoke on every targeted sub-agent.
            per_agent_args: Mapping of sub-agent name to its per-call args.
                Must be non-empty; an empty mapping is almost always a
                caller bug (likely a misnamed variable) and would silently
                produce an empty result dict. An empty mapping raises
                ValueError so the bug surfaces immediately.

        Returns:
            Dict mapping sub-agent name to the per-call result (or to the
            exception if that sub-agent's call failed; return_exceptions=True
            means partial failures are reported, not raised).

        Raises:
            ValueError: If ``per_agent_args`` is empty.
            KeyError: If any key in ``per_agent_args`` is not a known
                sub-agent of this fleet.
        """
        if not per_agent_args:
            raise ValueError(
                "fan_out requires a non-empty per_agent_args mapping; "
                "got an empty dict (this is almost always a caller bug)."
            )
        coros: list[Awaitable[Any]] = []
        names: list[str] = []
        for name, args in per_agent_args.items():
            if name not in self._agents:
                raise KeyError(f"Unknown sub-agent: {name}")
            coros.append(self._agents[name].call(tool, args))
            names.append(name)
        results = await asyncio.gather(*coros, return_exceptions=True)
        return {n: r for n, r in zip(names, results)}
