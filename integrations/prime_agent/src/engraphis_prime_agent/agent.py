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
from .tools import ToolFn, all_tools, build_tool, TOOL_SPECS

_logger = logging.getLogger("engraphis_prime_agent.agent")


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
        self.workspace = workspace or config.default_workspace
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
        self.goal = goal
        self.token_budget = token_budget
        self._session_id: str | None = None
        self._session_lock = asyncio.Lock()
        self._tools: dict[str, tuple[ToolFn, dict[str, Any]]] | None = None
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

    async def start_session(self, *, force_new: bool = False) -> str:
        # The two state mutations below happen under _session_lock so they
        # are atomic w.r.t. concurrent start_session / end_session callers
        # (and concurrent get_tool() callers that read self._session_id).
        async with self._session_lock:
            if self._session_id and not force_new:
                return self._session_id
            args: dict[str, Any] = {
                "action": "start",
                "agent": self.name,
                "force_new": force_new,
                "goal": self.goal,
                "token_budget": self.token_budget,
            }
            if self.workspace:
                args["workspace"] = self.workspace
            if self.repo:
                args["repo"] = self.repo
            response = await self.client.call_tool("engraphis_session", args)
            session_id = self._extract_session_id(response)
            if not session_id:
                raise EngraphisMcpToolError(
                    f"engraphis_session(start) for agent={self.name!r} returned no session_id."
                )
            # Atomic state transition: only one writer holds this lock.
            self._session_id = session_id
            self._tools = None  # rebuild bindings with the new session id
            return session_id

    async def end_session(self, *, summary: str = "", outcome: str = "") -> None:
        # Capture the id under the lock so a concurrent start_session can't
        # race us between the "no session" check and the call_tool.
        async with self._session_lock:
            session_id = self._session_id
            if not session_id:
                return
            # Always clear local state, even if the gateway call fails, so
            # the sub-agent is not stuck in a half-open state.
            self._session_id = None
            self._tools = None
        # Make the close-call best-effort. Log the error so operators can
        # spot stranded sessions, but never propagate: end_session() is
        # called from aclose/__aexit__ paths where raising would mask the
        # real shutdown error.
        try:
            await self.client.call_tool(
                "engraphis_session",
                {
                    "action": "end",
                    "agent": self.name,
                    "session_id": session_id,
                    "summary": summary,
                    "outcome": outcome,
                },
            )
        except Exception as exc:  # noqa: BLE001 — best-effort close
            _logger.warning(
                "end_session for agent=%r (session_id=%r) failed: %s",
                self.name,
                session_id,
                exc,
            )

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
        with self._tools_lock:
            if self._tools is None:
                self._tools = {
                    meta["name"]: build_tool(
                        meta["name"],
                        self.client,
                        self.config,
                        session_id=self._session_id,
                    )
                    for _fn, meta in all_tools(
                        self.client, self.config, session_id=self._session_id
                    )
                }
        return self._tools

    def tools(self) -> list[tuple[ToolFn, dict[str, Any]]]:
        bindings = self._ensure_tools()
        return [bindings[name] for name, _schema in TOOL_SPECS]

    def get_tool(self, name: str) -> tuple[ToolFn, dict[str, Any]]:
        return self._ensure_tools()[name]

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self._session_id:
            await self.start_session()
        fn, _schema = self.get_tool(tool)
        return await fn(args)

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
        ``call()``) still get a per-agent session injected.
        """
        agent = self

        async def _wrapper(args: dict[str, Any]) -> dict[str, Any]:
            if not agent._session_id:
                await agent.start_session()
                # start_session() rebuilds the bound tools with the new
                # session_id, so re-fetch the fresh binding for the current
                # call.
                fresh_fn, _schema = agent.get_tool(tool_name)
                return await fresh_fn(args)
            return await bound_fn(args)

        return _wrapper

    def status(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "workspace": self.workspace,
            "repo": self.repo,
            "goal": self.goal,
            "session_id": self._session_id,
            "tools_bound": self._tools is not None,
        }

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
        self._stack = AsyncExitStack()
        await self._stack.enter_async_context(self._client)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        # Best-effort: end every active session, then close the stdio gateway.
        await asyncio.gather(
            *(a.end_session() for a in self._agents.values()),
            return_exceptions=True,
        )
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        self._closed = True

    async def aclose(self) -> None:
        if not self._closed:
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
