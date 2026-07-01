"""Offline tests for the server-side workspace binding — the hard multi-tenant isolation
boundary (MASTER_PLAN.md §16: scope is "enforced server-side on every read/write — never
trust client-supplied scope alone").

Before this, `recall`/`why`/`timeline`/`recall_proactive` took the caller's asserted
`workspace` at face value: any MCP client that knew or guessed a workspace name could read
it (SECURITY.md §3). These tests pin the fix: when a MemoryService instance is *bound* to a
set of workspaces (``ENGRAPHIS_WORKSPACES``), every scoped read and write outside that set is
refused — and an *unbound* instance keeps the previous single-tenant behavior unchanged.

numpy-only, no model download, no mcp: two services are built over one shared engine so a
memory written under one workspace can be probed through a differently-bound service.
"""
import pytest

from engraphis.config import _parse_csv
from engraphis.service import MemoryService, ValidationError


def _bound(engine, allowed):
    return MemoryService(engine, allowed_workspaces=allowed)


def test_parse_csv_helper():
    assert _parse_csv("") == []
    assert _parse_csv("   ") == []
    assert _parse_csv("alpha, beta ,, gamma ") == ["alpha", "beta", "gamma"]


def test_empty_binding_is_unrestricted():
    """An empty allow-list (the default when ENGRAPHIS_WORKSPACES is unset) must behave
    exactly like no binding at all, so existing single-tenant installs are unaffected."""
    s = MemoryService.create(":memory:", allowed_workspaces=[])
    assert s.allowed_workspaces is None
    s.remember("anything goes here", workspace="whatever")  # must not raise


def test_unbound_instance_is_unrestricted():
    s = MemoryService.create(":memory:")  # no binding
    assert s.allowed_workspaces is None
    s.remember("alpha fact about widgets", workspace="alpha")
    s.remember("beta fact about gadgets", workspace="beta")
    # can read any named workspace, and workspace-less (global) reads still work
    assert s.recall("fact", workspace="beta")["count"] >= 1
    assert s.recall("fact")["count"] >= 1
    assert s.stats()["memories"] >= 2


def test_bound_instance_allows_its_own_workspace():
    seed = MemoryService.create(":memory:")
    seed.remember("alpha widget policy", workspace="alpha", repo="r")
    bound = _bound(seed.engine, ["alpha"])
    assert bound.recall("policy", workspace="alpha")["count"] >= 1
    # these must resolve without raising for the permitted workspace
    bound.why("policy", workspace="alpha")
    bound.timeline("policy", workspace="alpha")
    bound.recall_proactive(workspace="alpha")
    assert bound.stats(workspace="alpha")["memories"] >= 1


def test_bound_instance_blocks_reads_of_other_workspace():
    """The headline gap: a caller that knows the real name of another tenant's workspace
    still cannot read it once the instance is bound elsewhere."""
    seed = MemoryService.create(":memory:")
    seed.remember("Quarterly revenue is confidential.", workspace="beta", repo="fin")
    attacker = _bound(seed.engine, ["alpha"])  # bound to alpha, but knows "beta" exists
    for call in (
        lambda: attacker.recall("revenue", workspace="beta"),
        lambda: attacker.why("revenue", workspace="beta"),
        lambda: attacker.timeline("revenue", workspace="beta"),
        lambda: attacker.recall_proactive(workspace="beta"),
        lambda: attacker.stats(workspace="beta"),
    ):
        with pytest.raises(ValidationError):
            call()


def test_bound_instance_blocks_writes_and_governance_of_other_workspace():
    seed = MemoryService.create(":memory:")
    mid = seed.remember("beta secret", workspace="beta", repo="r")["id"]
    bound = _bound(seed.engine, ["alpha"])
    for call in (
        lambda: bound.remember("new beta fact", workspace="beta"),
        lambda: bound.forget(mid, workspace="beta"),
        lambda: bound.pin(mid, workspace="beta"),
        lambda: bound.correct(mid, "changed", workspace="beta"),
        lambda: bound.record_event("note", "x", workspace="beta"),
        lambda: bound.link("a", "b", workspace="beta"),
        lambda: bound.index_repo(workspace="beta", repo="r", root_path="/tmp/x"),
        lambda: bound.search_code("q", workspace="beta", repo="r"),
    ):
        with pytest.raises(ValidationError):
            call()


def test_bound_instance_cannot_reach_foreign_id_via_its_own_workspace():
    """Naming an allowed workspace must not launder access to a memory id that lives in a
    different one — the existing _check_owns guard still applies underneath the binding."""
    seed = MemoryService.create(":memory:")
    mid = seed.remember("beta secret", workspace="beta", repo="r")["id"]
    bound = _bound(seed.engine, ["alpha"])
    seed.remember("alpha fact", workspace="alpha", repo="r")  # make "alpha" exist
    with pytest.raises(ValidationError):
        bound.forget(mid, workspace="alpha")  # authorized workspace, foreign id


def test_bound_instance_refuses_workspaceless_global_ops():
    seed = MemoryService.create(":memory:")
    seed.remember("x", workspace="alpha")
    bound = _bound(seed.engine, ["alpha"])
    with pytest.raises(ValidationError):
        bound.recall("x")  # global recall would cross the boundary
    with pytest.raises(ValidationError):
        bound.stats()  # global aggregate counts would leak other tenants
