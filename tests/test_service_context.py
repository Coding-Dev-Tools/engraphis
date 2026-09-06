"""Concurrent embedding contexts must never inherit another tenant's service."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from engraphis.service import MemoryService, current_user
from engraphis.service_context import bind_service, bound_service, require_service_context


def test_required_context_cannot_fall_back_to_local_service():
    with require_service_context(), pytest.raises(RuntimeError, match="context is required"):
        bound_service()
    assert bound_service() is None


def test_contexts_keep_identical_workspace_names_isolated():
    services = [MemoryService.create(":memory:", extractor="none") for _ in range(2)]
    barrier = Barrier(2)

    def work(index):
        principal = {"id": "member_%d" % index, "email": "u%d@example.test" % index,
                     "role": "member"}
        with bind_service(services[index], principal=principal):
            bound_service().remember("Only tenant %d" % index, workspace="shared")
            barrier.wait(timeout=5)
            assert bound_service() is services[index]
            assert current_user()["id"] == principal["id"]
        assert bound_service() is None
        assert current_user() is None

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(work, range(2)))
    finally:
        for instance in services:
            instance.close()


def test_http_adapter_requires_explicit_context_when_requested():
    pytest.importorskip("fastapi", reason="HTTP adapter requires the optional server extra")
    from engraphis.routes.v2_api import service

    with require_service_context(), pytest.raises(RuntimeError, match="context is required"):
        service()
    instance = MemoryService.create(":memory:", extractor="none")
    principal = {"id": "member_http", "email": "http@example.test", "role": "member"}
    try:
        with bind_service(instance, principal=principal):
            assert service() is instance
    finally:
        instance.close()


def test_exception_and_nested_binding_restore_outer_identity():
    first = MemoryService.create(":memory:", extractor="none")
    second = MemoryService.create(":memory:", extractor="none")
    user = {"id": "member_outer", "email": "outer@example.test", "role": "member"}
    other = {"id": "member_inner", "email": "inner@example.test", "role": "viewer"}
    try:
        with bind_service(first, principal=user):
            with pytest.raises(ValueError, match="operation failed"):
                with bind_service(second, principal=other):
                    raise ValueError("operation failed")
            assert bound_service() is first
            assert current_user()["id"] == user["id"]
        assert current_user() is None
    finally:
        first.close()
        second.close()
