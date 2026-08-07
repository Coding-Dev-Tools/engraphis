"""Regression coverage for the dashboard's process-wide v2 service binding."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")

from engraphis.routes import v2_api  # noqa: E402


class _Store:
    def __init__(self, error: Optional[Exception] = None) -> None:
        self.error = error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


def test_service_binding_keeps_the_prior_service_when_close_fails(monkeypatch) -> None:
    prior_store = _Store(OSError("database handle is busy"))
    prior = SimpleNamespace(store=prior_store)
    replacement = SimpleNamespace(store=_Store())
    monkeypatch.setattr(v2_api, "_service", prior)

    with pytest.raises(RuntimeError, match="prior memory service could not be closed"):
        v2_api.set_service(replacement)

    assert prior_store.close_calls == 1
    assert v2_api._service is prior


def test_service_binding_closes_before_clearing(monkeypatch) -> None:
    prior_store = _Store()
    prior = SimpleNamespace(store=prior_store)
    monkeypatch.setattr(v2_api, "_service", prior)

    v2_api.set_service(None)

    assert prior_store.close_calls == 1
    assert v2_api._service is None


def test_rebinding_the_same_service_is_a_noop(monkeypatch) -> None:
    store = _Store()
    bound = SimpleNamespace(store=store)
    monkeypatch.setattr(v2_api, "_service", bound)

    v2_api.set_service(bound)

    assert store.close_calls == 0
    assert v2_api._service is bound
