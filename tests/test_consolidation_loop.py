"""Background-loop auto-consolidation (Phase 3) tests.

Contract under test (``engraphis/app.py::_consciousness_loop``):

* The sweep is **opt-in** via ``ENGRAPHIS_LOOP_CONSOLIDATE``; with the flag off the loop
  never calls consolidation, no matter how many candidates exist.
* When enabled, a cheap candidate pre-check runs first; with nothing to consolidate the
  expensive sweep is never invoked.
* When enabled and candidates exist, the sweep runs exactly once per eligible tick.
* The sweep runs in a worker thread (``asyncio.to_thread``) so the event loop is never
  blocked by the blocking cluster scan.
* Any exception raised by the sweep is swallowed and logged — the loop keeps running.

The legacy loop also performs decay and thought synthesis on every tick, so these tests
stub both out and drive the loop directly instead of booting an ASGI app.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from engraphis.config import settings

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from engraphis import app as app_module  # noqa: E402
from engraphis.core.interfaces import MemoryType  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402
from engraphis.service import MemoryService  # noqa: E402


def _service_with_candidates() -> MemoryService:
    """A service whose workspace holds consolidation-eligible episodic memories."""
    svc = MemoryService.create(":memory:")
    wid = svc.store.get_or_create_workspace("acme")
    rid = svc.store.get_or_create_repo(wid, "web")
    for run in (101, 202, 303):
        svc.engine.remember(
            f"Build failed on the flaky network integration test in CI run {run}.",
            workspace_id=wid, repo_id=rid, mtype=MemoryType.EPISODIC,
            resolve_conflicts=False,
        )
    return svc


def _service_without_candidates() -> MemoryService:
    """An empty service: no memories at all, so the pre-check must short-circuit."""
    return MemoryService.create(":memory:")


def _stub_loop_dependencies(monkeypatch) -> None:
    """Neutralize the per-tick decay + thought synthesis stages."""
    monkeypatch.setattr(app_module.reweight, "decay_pass", lambda namespace=None: 0)
    monkeypatch.setattr(app_module.thoughts_engine, "synthesize_thoughts",
                        lambda **kw: {"persisted": False})


def _loop_until(monkeypatch, ticks: int) -> None:
    """Run ``_consciousness_loop`` for exactly ``ticks`` full iterations.

    The loop is an infinite ``while True``, so the test drives it with a patched
    ``asyncio.sleep`` that raises ``CancelledError`` once ``ticks`` sleep calls have
    happened. The loop re-raises ``CancelledError`` by design (its shutdown path), so
    the coroutine unwinds cleanly and ``asyncio.run`` surfaces it; the helper swallows
    it. Any other exception propagating out of the loop fails the test.
    """
    state = {"sleeps": 0}
    real_sleep = asyncio.sleep

    async def counting_sleep(delay):
        state["sleeps"] += 1
        if state["sleeps"] > ticks:
            raise asyncio.CancelledError()
        await real_sleep(0)

    monkeypatch.setattr(app_module.asyncio, "sleep", counting_sleep)
    try:
        asyncio.run(app_module._consciousness_loop())
    except asyncio.CancelledError:
        pass  # the expected stop signal after ``ticks`` iterations


def test_loop_flag_off_never_calls_consolidate(monkeypatch):
    """With ENGRAPHIS_LOOP_CONSOLIDATE=0 the sweep is never invoked — not even the
    candidate pre-check — regardless of how many candidates exist."""
    _stub_loop_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 0)
    svc = _service_with_candidates()
    v2_api.set_service(svc)

    sweep_calls: list = []
    monkeypatch.setattr(app_module, "_run_loop_consolidation",
                        lambda engine: sweep_calls.append(engine))
    precheck_calls: list = []
    monkeypatch.setattr(app_module, "_consolidation_candidates_exist",
                        lambda engine: precheck_calls.append(engine) or True)

    _loop_until(monkeypatch, ticks=1)

    assert sweep_calls == []
    assert precheck_calls == []
    v2_api.set_service(None)


def test_loop_enabled_without_candidates_skips_sweep(monkeypatch):
    """Enabled but nothing to consolidate: the real pre-check runs, the sweep does not."""
    _stub_loop_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 1)
    v2_api.set_service(_service_without_candidates())

    sweep_calls: list = []
    monkeypatch.setattr(app_module, "_run_loop_consolidation",
                        lambda engine: sweep_calls.append(engine))

    _loop_until(monkeypatch, ticks=1)

    assert sweep_calls == []
    v2_api.set_service(None)


def test_loop_enabled_with_candidates_calls_sweep_exactly_once(monkeypatch):
    """Enabled and candidates exist: exactly one sweep invocation per eligible tick."""
    _stub_loop_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 1)
    svc = _service_with_candidates()
    v2_api.set_service(svc)

    sweep_calls: list = []
    monkeypatch.setattr(app_module, "_run_loop_consolidation",
                        lambda engine: sweep_calls.append(engine))

    _loop_until(monkeypatch, ticks=1)

    assert len(sweep_calls) == 1
    assert sweep_calls[0] is svc.engine
    v2_api.set_service(None)


def test_loop_sweep_exception_is_swallowed_and_loop_continues(monkeypatch):
    """An exception inside the sweep must not escape: the next tick still runs and
    calls the sweep again (a leaked exception would skip the second tick entirely)."""
    _stub_loop_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 1)
    v2_api.set_service(_service_with_candidates())

    sweep_calls: list = []

    def boom(engine):
        sweep_calls.append(engine)
        raise RuntimeError("sweep exploded")

    monkeypatch.setattr(app_module, "_run_loop_consolidation", boom)

    # Two full ticks: tick 1 raises inside the sweep, tick 2 must still run.
    _loop_until(monkeypatch, ticks=2)

    assert len(sweep_calls) == 2  # the failure did not stop the loop
    v2_api.set_service(None)


def test_sweep_runs_in_a_worker_thread_not_the_event_loop(monkeypatch):
    """The blocking sweep is dispatched via asyncio.to_thread, so the thread that runs
    it is not the event-loop thread."""
    _stub_loop_dependencies(monkeypatch)
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "loop_consolidate", 1)
    v2_api.set_service(_service_with_candidates())

    observed: dict = {}

    def record_thread(engine):
        observed["sweep_thread"] = threading.current_thread()

    monkeypatch.setattr(app_module, "_run_loop_consolidation", record_thread)

    loop_thread_ident = threading.current_thread().ident  # asyncio.run runs in this thread
    _loop_until(monkeypatch, ticks=1)

    assert observed["sweep_thread"] is not None
    assert observed["sweep_thread"].ident != loop_thread_ident
    v2_api.set_service(None)
