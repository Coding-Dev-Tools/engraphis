"""Dreaming trigger — idle + accumulation gating for automated consolidation.

The trigger is purely additive to the cadence (`due`): it can only cause a sweep to
run *earlier* when enough new episodic memories have accumulated and the store has gone
quiet. These tests pin the pure decision logic and the store-backed `dream_due` wrapper.
"""
import time
from types import SimpleNamespace

from engraphis import automation
from engraphis.service import MemoryService


def _mem(ts, mtype="episodic"):
    return SimpleNamespace(ingested_at=ts, mtype=mtype)


def _policy(**over):
    p = automation.normalize_policy({"enabled": True, **over})
    p["last_run"] = over.get("last_run", None)
    return p


def test_normalize_fills_dream_defaults():
    p = automation.normalize_policy({})
    assert p["dream"] is True and p["dream_min_new"] == 25 and p["dream_idle_minutes"] == 15


def test_normalize_preserves_and_clamps_explicit_dream_values():
    p = automation.normalize_policy(
        {"dream": False, "dream_min_new": 5, "dream_idle_minutes": 0})
    assert p["dream"] is False and p["dream_min_new"] == 5 and p["dream_idle_minutes"] == 0
    # garbage / out-of-range coerces to safe defaults, never raises
    bad = automation.normalize_policy({"dream_min_new": "x", "dream_idle_minutes": -9})
    assert bad["dream_min_new"] == 25 and bad["dream_idle_minutes"] == 0


def test_dream_signals_counts_new_episodics_and_idle():
    now = 10_000.0
    mems = [_mem(now - 60, "episodic"),      # new (after last_run), episodic
            _mem(now - 30, "semantic"),      # not episodic
            _mem(now - 5_000, "episodic")]   # old (before last_run)
    new_ep, idle = automation.dream_signals(mems, last_run=now - 100, now=now)
    assert new_ep == 1
    assert idle == 30  # newest write was 30s ago


def test_should_dream_requires_accumulation_and_idle():
    now = 10_000.0
    quiet = [_mem(now - 3_600) for _ in range(30)]        # 30 new episodics, 1h idle
    busy = [_mem(now - 10) for _ in range(30)]            # plenty new, but just written
    thin = [_mem(now - 3_600) for _ in range(3)]          # idle, but too few
    pol = _policy(dream_min_new=25, dream_idle_minutes=15)
    assert automation.should_dream(pol, quiet, now=now) is True
    assert automation.should_dream(pol, busy, now=now) is False   # not idle yet
    assert automation.should_dream(pol, thin, now=now) is False   # not enough new


def test_should_dream_off_when_disabled_or_dreaming_off():
    now = 10_000.0
    quiet = [_mem(now - 3_600) for _ in range(30)]
    assert automation.should_dream(automation.normalize_policy({"enabled": False}),
                                   quiet, now=now) is False
    assert automation.should_dream(_policy(dream=False), quiet, now=now) is False


def test_dream_due_fires_early_on_accumulation_but_not_on_thin_store():
    svc = MemoryService.create(":memory:")
    t0 = time.time()
    # Distinct events so the write-path resolver doesn't dedup them into one memory.
    events = ["Deployed the auth service to production",
              "Rotated the vault signing key on schedule",
              "Merged the billing seat-pricing pull request",
              "Investigated a latency spike in the recall path"]
    for e in events:
        svc.remember(e, workspace="ws", mtype="episodic")
    now = t0 + 1
    base = dict(last_run=t0 - 3600, cadence_hours=24, dream_idle_minutes=0)  # cadence NOT due

    fires = _policy(dream_min_new=3, **base)
    assert automation.dream_due(svc, policy=fires, now=now) is True          # accumulation

    quiet = _policy(dream_min_new=100, **base)
    assert automation.dream_due(svc, policy=quiet, now=now) is False         # too few new


def test_dream_due_still_fires_on_cadence_regardless_of_accumulation():
    svc = MemoryService.create(":memory:")
    now = time.time()
    # cadence elapsed (last run 25h ago) → due() path, even with an impossible dream gate
    pol = _policy(last_run=now - 25 * 3600, cadence_hours=24, dream_min_new=10_000)
    assert automation.dream_due(svc, policy=pol, now=now) is True


def test_dream_due_scopes_accumulation_to_policy_workspaces():
    # The trigger must only count accumulation *inside* the scoped workspaces: a burst in
    # an out-of-scope workspace must not fire a sweep, while one in a scoped workspace does.
    svc = MemoryService.create(":memory:")
    svc.store.get_or_create_workspace("a")
    svc.store.get_or_create_workspace("b")
    burst = ("Deployed the auth service to production",
             "Rotated the vault signing key on schedule",
             "Merged the billing seat-pricing pull request",
             "Investigated a latency spike in the recall path",
             "Shipped the onboarding wizard to staging")
    for e in burst:                          # a real burst, but in b only — distinct subjects
        svc.remember(e, workspace="b", mtype="episodic")
    t0 = time.time()
    now = t0 + 60   # comfortably after all the writes below so idle is positive
    base = dict(last_run=t0 - 3600, cadence_hours=24, dream_idle_minutes=0)
    # unscoped: b's burst alone fires the trigger (proves the burst is real)
    assert automation.dream_due(svc, policy=_policy(dream_min_new=3, **base), now=now) is True
    # scoped to a: b's burst is ignored → no fire
    only_a = _policy(dream_min_new=3, workspaces=["a"], **base)
    assert automation.dream_due(svc, policy=only_a, now=now) is False
    for e in ("Refactored the recall fusion weights",           # now accumulate inside a —
              "Added a circuit breaker to the gateway",         # distinct subjects so the
              "Documented the PASETO rotation runbook",         # resolver keeps all five
              "Fixed the webhook idempotency bug",
              "Tuned the BM25 index saturation"):
        svc.remember(e, workspace="a", mtype="episodic")
    assert automation.dream_due(svc, policy=only_a, now=now) is True
    # an unknown workspace name in the policy is skipped, not created, and never crashes
    only_ghost = _policy(dream_min_new=3, workspaces=["nope"], **base)
    assert automation.dream_due(svc, policy=only_ghost, now=now) is False
