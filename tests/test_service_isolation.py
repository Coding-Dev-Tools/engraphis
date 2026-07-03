"""Cross-tenant isolation tests for MemoryService — the server-side workspace binding.

Locks in the guarantee that a MemoryService bound to a set of workspaces
(``ENGRAPHIS_WORKSPACES``) refuses every read AND write outside that set — knowing the
name of another tenant's workspace is not enough (``service.py:_authorize_workspace``,
MASTER_PLAN.md §16: scope is "enforced server-side on every read/write — never trust
client-supplied scope alone").

Writes were always guarded; the read paths (recall / why / timeline / stats /
list_workspaces) are the ones that historically were not, so they get the coverage here.
This is the first test over ``service.py`` at all — the isolation boundary was previously
unpinned, so a refactor could have silently reopened it.

Offline only: DeterministicEmbedder + NumpyVectorIndex, no torch (AGENTS.md §3).
"""
import pytest

from engraphis.service import MemoryService, ValidationError


def _bound(*workspaces):
    """A MemoryService bound to ``workspaces`` — the multi-tenant hard boundary."""
    return MemoryService.create(":memory:", allowed_workspaces=list(workspaces))


# ── writes are refused outside the binding (regression guard; was already enforced) ──
def test_remember_rejects_unpermitted_workspace():
    with pytest.raises(ValidationError):
        _bound("team-a").remember("secret", workspace="team-b", repo="r1")


# ── reads are refused outside the binding (the historically weak paths) ──────────────
def test_recall_rejects_unpermitted_workspace():
    with pytest.raises(ValidationError):
        _bound("team-a").recall("q", workspace="team-b")


def test_recall_without_workspace_is_refused_on_a_bound_instance():
    # A workspace-less recall on a bound instance would read across every tenant — the
    # exact boundary the binding exists to enforce.
    with pytest.raises(ValidationError):
        _bound("team-a").recall("q")


def test_why_rejects_unpermitted_workspace():
    with pytest.raises(ValidationError):
        _bound("team-a").why("q", workspace="team-b")


def test_timeline_rejects_unpermitted_workspace():
    with pytest.raises(ValidationError):
        _bound("team-a").timeline("q", workspace="team-b")


def test_stats_without_workspace_is_refused_on_a_bound_instance():
    with pytest.raises(ValidationError):
        _bound("team-a").stats()


# ── the binding permits its own workspace, and reads its own data back ───────────────
def test_bound_instance_allows_and_reads_its_own_workspace():
    svc = _bound("team-a")
    stored = svc.remember("alpha fact", workspace="team-a", repo="r1")
    assert stored["stored"] is True
    hit = svc.recall("alpha fact", workspace="team-a", repo="r1")
    assert hit["count"] >= 1


def test_bound_list_workspaces_shows_only_permitted():
    svc = _bound("team-a")
    svc.remember("alpha fact", workspace="team-a", repo="r1")
    names = {w["name"] for w in svc.list_workspaces()["workspaces"]}
    assert names == {"team-a"}   # never leaks a workspace outside the binding (service.py:486)


# ── the unbound single-tenant default stays global (no accidental lockout) ───────────
def test_unbound_instance_permits_global_recall():
    svc = MemoryService.create(":memory:")      # no binding
    svc.remember("alpha fact", workspace="team-a", repo="r1")
    out = svc.recall("alpha fact")              # workspace-less recall is allowed when unbound
    assert "memories" in out
