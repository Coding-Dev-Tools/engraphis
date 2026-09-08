"""Request-scoped service injection for applications embedding the local engine.

The standalone entry points retain their local default. A hosted application must
enter ``bind_service`` for each operation, with a validated principal. Contexts
are restored even when an operation fails, and never mutate module singletons.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Iterator, Optional

if TYPE_CHECKING:
    from engraphis.service import MemoryService

_BOUND: ContextVar[Optional["MemoryService"]] = ContextVar("engraphis_bound_service", default=None)
_REQUIRED: ContextVar[bool] = ContextVar("engraphis_bound_service_required", default=False)


def bound_service() -> Optional["MemoryService"]:
    """Resolve an injected service, refusing an absent required binding."""
    result = _BOUND.get()
    if result is None and _REQUIRED.get():
        raise RuntimeError("An authenticated service context is required")
    return result


@contextmanager
def require_service_context() -> Iterator[None]:
    """Disable the standalone fallback for the duration of a hosted request."""
    token = _REQUIRED.set(True)
    try:
        yield
    finally:
        _REQUIRED.reset(token)


@contextmanager
def bind_service(service: "MemoryService", *, principal: dict) -> Iterator["MemoryService"]:
    """Bind an explicit service and principal, restoring the enclosing context."""
    from engraphis.service import _CURRENT_USER, set_current_user

    if service is None or not principal:
        raise ValueError("An explicit service and authenticated principal are required")
    previous_user = _CURRENT_USER.get()
    try:
        set_current_user(principal)
    except Exception:
        _CURRENT_USER.set(previous_user)
        raise
    service_token = _BOUND.set(service)
    required_token = _REQUIRED.set(True)
    try:
        yield service
    finally:
        _REQUIRED.reset(required_token)
        _BOUND.reset(service_token)
        _CURRENT_USER.set(previous_user)
