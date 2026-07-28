"""Launch guards for the public client: no silent hangs, no unvetted probe, no false locks.

Each test pins a defect that reached a customer's machine:

* ``engraphis-update`` shelled out to git/pip/pipx with no ``timeout=`` and
  ``capture_output=True``, so a stalled index or unreachable remote hung forever with
  nothing on screen — and a stall *after* the release tag was checked out skipped the
  rollback and left a wedged half-upgrade;
* the update probe was the one credential-path-adjacent HTTP call using a plain
  ``build_opener``, skipping the repo's SSRF / DNS-rebinding vetting on an endpoint that
  ``ENGRAPHIS_UPDATE_URL`` makes operator-controllable; and
* ``/api/license`` always returned ``features: []``, so a paying Pro or Team customer saw
  "PRO"/"TEAM" lock badges on the features they had just bought.

*Which* plan that feature list is computed from is pinned separately, in
tests/test_hosted_plan_resolution.py: this file owns the plan → feature table and the
dashboard's lock-badge loop; that one owns how the plan itself is learned from the control
plane, cached, and degraded offline.
"""
from __future__ import annotations

import ast
import inspect
import re
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

# ``engraphis.routes.v2_api`` imports FastAPI, which the numpy-only core floor job does not
# install. Skip rather than error at collection, matching the rest of the suite.
pytest.importorskip("fastapi", reason="full-stack extra not installed")

from engraphis import hosted_client, update_check  # noqa: E402
from engraphis.routes import v2_api  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPO_ROOT / "scripts" / "update.py"
DASHBOARD_JS = REPO_ROOT / "engraphis" / "static" / "dashboard.js"

# The private control plane's plan→feature table, mirrored from (read-only)
# the hosted entitlement contract plan-feature mapping. A drift here means a
# purchased capability silently renders as locked.
SERVER_HOSTED_ENTITLEMENTS = {
    "free": set(),
    "pro": {"analytics", "automation", "sync"},
    "team": {"analytics", "automation", "sync", "team"},
}
# Named separately by this client's commercial manifest; the server grants both under
# ``automation``, so any plan granting ``automation`` must grant these too.
CLIENT_AUTOMATION_ALIASES = {"consolidation", "dreaming"}


# ── (1) the updater must never hang on a stalled remote ───────────────────────
_SPAWNERS = ("subprocess.run", "subprocess.call", "subprocess.check_output",
             "subprocess.check_call", "subprocess.Popen")


def _drains_under_a_deadline(scope: ast.AST) -> bool:
    """True when *scope* reads its child's pipe with an explicit budget on the read."""

    return any(
        isinstance(node, ast.Call)
        and ast.unparse(node.func).endswith(".communicate")
        and any(keyword.arg == "timeout" for keyword in node.keywords)
        for node in ast.walk(scope)
    )


def test_the_updater_runs_no_unbounded_subprocess() -> None:
    """Every shell-out is bounded — including the ones whose output must be parsed.

    ``subprocess.run(capture_output=True, timeout=N)`` does not enforce ``N``: once the
    budget expires CPython kills the direct child and then drains the pipes with an
    *unbounded* ``communicate()``, which waits for every inherited write handle to close —
    grandchildren such as ``git-remote-https`` included. So a ``timeout=`` keyword is not
    on its own proof of a bound. A ``Popen`` is accepted only when the same function bounds
    the read that follows it; anything else is the old silent hang wearing a budget.
    """

    tree = ast.parse(UPDATER.read_text(encoding="utf-8"))
    enclosing = {}
    for scope in ast.walk(tree):
        if isinstance(scope, ast.FunctionDef):
            for node in ast.walk(scope):
                enclosing[node] = scope

    unbounded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target not in _SPAWNERS:
            continue
        if any(kw.arg == "timeout" for kw in node.keywords):
            continue
        scope = enclosing.get(node)
        if target == "subprocess.Popen" and scope is not None:
            if _drains_under_a_deadline(scope):
                continue
        unbounded.append(target)

    assert unbounded == [], "unbounded subprocess call(s) in scripts/update.py: %s" % unbounded


def test_the_updater_helper_makes_a_timeout_impossible_to_omit() -> None:
    """``timeout`` has no default, so a new call site cannot silently be unbounded."""

    import scripts.update as updater

    for helper in (updater._run, updater._run_captured):
        timeout = inspect.signature(helper).parameters["timeout"]
        assert timeout.default is inspect.Parameter.empty, helper.__name__

    tree = ast.parse(UPDATER.read_text(encoding="utf-8"))
    call_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") in ("_run", "_run_captured")
    ]
    assert call_sites, "expected the updater to shell out through _run"
    for node in call_sites:
        supplied = len(node.args) + len(node.keywords)
        assert supplied >= 3, ast.unparse(node)


def test_a_stalled_remote_aborts_instead_of_waiting_forever(monkeypatch) -> None:
    """The failing call is abandoned at its budget and reported, not awaited.

    ``ls-remote`` output has to be parsed, so this step cannot simply stop capturing. It
    therefore has to kill the *whole* process tree before it reads the pipe again, and
    bound that read too — otherwise the surviving ``git-remote-https`` still owns the write
    handle and the "budget" expires into an indefinite wait.
    """

    import scripts.update as updater

    seen = {"drains": []}

    class _Stalled:
        pid = 4242
        returncode = None

        def communicate(self, timeout=None):
            seen["drains"].append(timeout)
            raise subprocess.TimeoutExpired("git", timeout)

    def _spawn(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        seen["stdin"] = kwargs.get("stdin")
        seen["env"] = kwargs.get("env")
        return _Stalled()

    monkeypatch.setattr(updater.subprocess, "Popen", _spawn)
    monkeypatch.setattr(
        updater.subprocess, "run",
        lambda *a, **k: pytest.fail("a network query must not use the unenforceable path"),
    )
    monkeypatch.setattr(
        updater, "_kill_process_tree",
        lambda process: seen.__setitem__("killed", process.pid),
    )

    with pytest.raises(updater.UpdateTimeout) as excinfo:
        updater._remote_latest_tag("/usr/bin/git", "https://example.test/engraphis.git")

    assert isinstance(seen["drains"][0], (int, float)) and seen["drains"][0] > 0
    assert seen["killed"] == 4242, "the grandchildren must die before the pipe is re-read"
    assert len(seen["drains"]) == 2 and seen["drains"][1] > 0, "the drain is bounded too"
    # Nothing may stop to ask a human for a password on a machine nobody is watching.
    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["env"]["GIT_TERMINAL_PROMPT"] == "0"
    # A hang is only survivable if the user is told which step stalled and what to do.
    message = str(excinfo.value)
    assert "timed out" in message
    assert "engraphis-update" in message


def test_main_reports_a_stalled_step_and_exits(monkeypatch, capsys) -> None:
    """The stall must surface as a non-zero exit with copy, not an unhandled traceback."""

    import scripts.update as updater

    monkeypatch.setattr(updater, "_detect_install", lambda: "pypi")

    class _Stalled:
        pid = 5150
        returncode = None

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("pip", timeout)

    monkeypatch.setattr(updater.subprocess, "Popen", lambda cmd, **kwargs: _Stalled())
    monkeypatch.setattr(updater, "_kill_process_tree", lambda process: None)

    with pytest.raises(SystemExit) as excinfo:
        updater.main([])

    assert excinfo.value.code == 1
    assert "timed out" in capsys.readouterr().err


def test_a_stalled_reinstall_still_rolls_back_the_checkout(monkeypatch, tmp_path) -> None:
    """The regression that wedges an install.

    By the time the reinstall runs, the tree is already detached onto the new release
    tag. An unbounded reinstall therefore hangs *after* the destructive step and the
    rollback below it is never reached, stranding a working editable install on a
    half-applied upgrade. The timeout has to be catchable for the rollback to run at all.
    """

    import scripts.update as updater

    project = tmp_path / "engraphis"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setattr(updater, "LATEST_TAG", "")
    monkeypatch.setattr(updater.shutil, "which", lambda name: "/usr/bin/git")

    calls = []
    reinstalls = {"n": 0}

    class _Proc:
        pid = 909

        def __init__(self, returncode=0, stdout="", stall=False):
            self.returncode = returncode
            self._stdout = stdout
            self._stall = stall

        def communicate(self, timeout=None):
            assert timeout, "every step must carry a budget"
            if self._stall:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return self._stdout, None

    def _fake_popen(cmd, **kwargs):
        cmd = list(cmd)
        calls.append(cmd)
        if cmd[:4] == [sys.executable, "-m", "pip", "show"]:
            return _Proc(stdout="Editable project location: %s\n" % project)
        if cmd[:5] == [sys.executable, "-m", "pip", "install", "-e"]:
            reinstalls["n"] += 1
            # the upgrade reinstall stalls; the rollback reinstall succeeds
            return _Proc(stall=reinstalls["n"] == 1)
        if "ls-remote" in cmd:
            assert kwargs.get("stdout") is subprocess.PIPE, "the refs query must be parsed"
            return _Proc(stdout="%s\trefs/tags/v9.9.9\n" % ("b" * 40))
        if "rev-parse" in cmd:
            return _Proc(stdout="a" * 40 + "\n")
        if "symbolic-ref" in cmd:
            return _Proc(stdout="main\n")
        if "rev-list" in cmd:
            return _Proc(stdout="b" * 40 + "\n")
        if "status" in cmd:
            return _Proc(stdout="")
        return _Proc()

    monkeypatch.setattr(updater.subprocess, "Popen", _fake_popen)
    monkeypatch.setattr(
        updater.subprocess, "run",
        lambda *a, **k: pytest.fail("no step may spawn through the unenforceable path"),
    )
    # Never let a fabricated pid reach the real ``taskkill``/``killpg``.
    monkeypatch.setattr(updater, "_kill_process_tree", lambda process: None)

    with pytest.raises(updater.UpdateTimeout):
        updater._git_update()

    checkouts = [cmd for cmd in calls if "checkout" in cmd]
    assert ["/usr/bin/git", "-C", str(project), "checkout", "tags/v9.9.9"] in checkouts
    # The rollback ran despite the stall, and restored the original branch.
    assert ["/usr/bin/git", "-C", str(project), "checkout", "main"] in checkouts
    assert reinstalls["n"] == 2, "the previous checkout must be reinstalled"


# ── (2) the update probe must use the vetted connector ────────────────────────
def test_update_probe_uses_the_pinned_opener_with_a_timeout(monkeypatch) -> None:
    """``ENGRAPHIS_UPDATE_URL`` makes this endpoint operator-controllable.

    A plain ``build_opener`` dials whatever the hostname resolves to at connect time, so
    it neither rejects private/reserved targets nor closes the DNS-rebinding window
    between the scheme check and the connect.
    """

    used = {}

    def _fake_pinned(*handlers):
        used["handlers"] = handlers

        class _Opener:
            def open(self, request, timeout=None):
                used["timeout"] = timeout
                raise urllib.error.URLError("blocked")

        return _Opener()

    def _forbidden(*args, **kwargs):
        raise AssertionError("the update probe must not build an unvetted opener")

    monkeypatch.setattr(update_check, "build_pinned_https_opener", _fake_pinned)
    monkeypatch.setattr(update_check.urllib.request, "build_opener", _forbidden)

    assert update_check._fetch("https://mirror.example.test/latest.json", 4.0) is None
    assert used["handlers"], "the no-redirect handler must still be installed"
    assert used["timeout"] == 4.0


def test_update_probe_imports_the_repo_connector() -> None:
    """Pin the import so a refactor cannot quietly fall back to urllib's default."""

    assert update_check.build_pinned_https_opener is not None
    source = (REPO_ROOT / "engraphis" / "update_check.py").read_text(encoding="utf-8")
    assert "urllib.request.build_opener(" not in source


# ── (3) a paying customer must not see a lock on what they bought ─────────────
@pytest.mark.parametrize("plan", ["pro", "team"])
def test_a_paid_plan_grants_a_non_empty_feature_list(plan) -> None:
    features = v2_api.entitled_features(plan)

    assert features, "a paid plan must grant features"
    assert SERVER_HOSTED_ENTITLEMENTS[plan].issubset(features)
    # The server folds these into ``automation``; the client names them separately.
    assert CLIENT_AUTOMATION_ALIASES.issubset(features)


@pytest.mark.parametrize("plan", ["free", "local", "", None, "enterprise", "unknown", "pro-plus"])
def test_an_unpaid_or_unrecognised_plan_grants_nothing(plan) -> None:
    assert v2_api.entitled_features(plan) == []


@pytest.mark.parametrize("spelling", ["PRO", " pro ", "Pro", "\tpro\n"])
def test_plan_lookup_is_case_and_whitespace_insensitive(spelling) -> None:
    """The control plane emits lowercase plans; a caller must not be able to drift."""

    assert v2_api.entitled_features(spelling) == v2_api.entitled_features("pro")


def test_team_grants_everything_pro_does_plus_team() -> None:
    pro, team = set(v2_api.entitled_features("pro")), set(v2_api.entitled_features("team"))

    assert pro < team
    assert team - pro == {"team"}


def test_client_never_advertises_a_feature_the_server_cannot_grant() -> None:
    server_keys = set().union(*SERVER_HOSTED_ENTITLEMENTS.values())
    for plan in ("pro", "team"):
        extra = set(v2_api.entitled_features(plan)) - server_keys
        assert extra <= CLIENT_AUTOMATION_ALIASES, extra


def test_license_route_unlocks_a_connected_paying_customer(monkeypatch) -> None:
    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "team")

    payload = v2_api.get_license()

    assert payload["plan"] == "team"
    assert payload["features"], "a paying customer must not be sent an empty feature list"
    assert "team" in payload["features"]


def test_license_route_leaves_the_free_local_core_locked(monkeypatch) -> None:
    monkeypatch.delenv("ENGRAPHIS_CLOUD_PLAN", raising=False)
    monkeypatch.setattr(v2_api, "_hosted_plan", lambda: "local")

    payload = v2_api.get_license()

    assert payload["plan"] == "local"
    assert payload["features"] == []


def test_an_unconnected_installation_is_the_free_local_core(monkeypatch) -> None:
    from engraphis import cloud_session

    monkeypatch.delenv("ENGRAPHIS_CLOUD_PLAN", raising=False)
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: False)

    assert v2_api._hosted_plan() == "local"


def test_a_connected_installation_reports_a_paid_plan(monkeypatch) -> None:
    """The fallback before the control plane has ever answered.

    ``pro`` is the smallest paid plan, so a connected customer is never shown the free
    local core while the authoritative entitlement is still unknown. It is a floor, not
    the answer: once ``GET /v1/entitlements/{org}`` has been read, the cached plan wins
    (tests/test_hosted_plan_resolution.py).
    """

    from engraphis import cloud_session

    monkeypatch.delenv("ENGRAPHIS_CLOUD_PLAN", raising=False)
    monkeypatch.delenv("ENGRAPHIS_CLOUD_CONTROL_URL", raising=False)
    monkeypatch.setattr(cloud_session, "configured", lambda **kw: True)

    assert v2_api._hosted_plan() == "pro"
    # With no endpoint configured there is nothing to dial, so nothing is scheduled.
    assert v2_api._entitlement_refreshing is False


def test_an_unreadable_cloud_session_never_breaks_the_dashboard(monkeypatch) -> None:
    """``/api/license`` is on the ``/api/bootstrap`` path; a badge must not 500 the boot."""

    from engraphis import cloud_session

    def _boom(**kwargs):
        raise cloud_session.CloudSessionError("temporarily unreadable")

    monkeypatch.delenv("ENGRAPHIS_CLOUD_PLAN", raising=False)
    monkeypatch.setattr(cloud_session, "configured", _boom)

    assert v2_api._hosted_plan() == "local"
    assert v2_api.get_license()["features"] == []


def test_license_route_emits_every_field_the_dashboard_reads(monkeypatch) -> None:
    """The dashboard read these off the license payload; no route ever emitted them."""

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "pro")

    payload = v2_api.get_license()

    for field in (
        "plan", "features", "known_features", "is_trial", "trial",
        "access_state", "entitlement_status", "upgrade_url",
        "pro_upgrade_url", "team_upgrade_url",
        "pro_monthly_upgrade_url", "pro_annual_upgrade_url",
        "team_monthly_upgrade_url", "team_annual_upgrade_url",
        "account_url",
    ):
        assert field in payload, field
    # ``used`` gates every "Start your free trial" affordance; ``available`` is what stops
    # one being offered to a customer the control plane will answer 409 for; ``active`` and
    # ``ends_at`` are what let the panel name a live trial and its boundary.
    for field in ("used", "active", "available", "ends_at", "trial_days"):
        assert field in payload["trial"], field
    # Pro and Team bill through separate checkout targets.
    assert payload["pro_upgrade_url"] and payload["team_upgrade_url"]
    assert payload["account_url"]


def test_the_account_url_is_plan_neutral_where_the_checkouts_are_separate(
    monkeypatch,
) -> None:
    """"Open account portal" must not be the Pro checkout wearing a neutral name.

    ``licensing.upgrade_url()`` takes no argument here but resolves ``plan="pro"`` inside,
    so it prefers ``ENGRAPHIS_PRO_UPGRADE_URL`` — which is exactly the page a lapsed
    customer with a payment-method problem must not be sent to. ``account_url`` resolves
    the generic value directly.
    """

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "pro")
    monkeypatch.setenv("ENGRAPHIS_UPGRADE_URL", "https://cloud.test/account")
    monkeypatch.setenv("ENGRAPHIS_PRO_UPGRADE_URL", "https://cloud.test/checkout/pro")
    monkeypatch.setenv("ENGRAPHIS_TEAM_UPGRADE_URL", "https://cloud.test/checkout/team")

    payload = v2_api.get_license()

    assert payload["account_url"] == "https://cloud.test/account"
    assert payload["pro_upgrade_url"] == "https://cloud.test/checkout/pro"
    assert payload["team_upgrade_url"] == "https://cloud.test/checkout/team"
    # The regression itself: the generic key really is the Pro checkout here.
    assert payload["upgrade_url"] == "https://cloud.test/checkout/pro"

    # With nothing configured it falls back to the hosted account root, never to a plan.
    monkeypatch.delenv("ENGRAPHIS_UPGRADE_URL")
    assert v2_api.get_license()["account_url"] == hosted_client.DEFAULT_CLOUD_URL
    # Every advertised key must be renderable, and every grantable key advertised.
    assert set(payload["features"]) <= set(payload["known_features"])
    assert set(v2_api.entitled_features("team")) == set(payload["known_features"])


def test_dashboard_lock_badges_clear_for_a_paid_plan(monkeypatch) -> None:
    """Close the loop on the JS: ``locked = !features.includes(f)``.

    The dashboard's three gated nav items are read straight out of the shipped asset so a
    renamed feature key cannot re-lock a paid customer without failing here.
    """

    script = DASHBOARD_JS.read_text(encoding="utf-8")
    block = script[script.index("function updateFeatureLocks()"):]
    block = block[:block.index("\n}")]
    gated = re.findall(r"apply\('[^']+',\s*'([^']+)'", block)

    assert set(gated) == {"analytics", "automation", "team"}, gated

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "team")
    team_features = v2_api.get_license()["features"]
    for feature in gated:
        assert feature in team_features, "Team still renders a lock on %s" % feature

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "pro")
    pro_features = v2_api.get_license()["features"]
    assert {"analytics", "automation"} <= set(pro_features)
    assert "team" not in pro_features  # Team upsell stays visible on a Pro plan

    monkeypatch.setenv("ENGRAPHIS_CLOUD_PLAN", "free")
    assert v2_api.get_license()["features"] == []
