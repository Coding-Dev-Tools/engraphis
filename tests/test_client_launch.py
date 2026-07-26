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

from engraphis import update_check
from engraphis.routes import v2_api

REPO_ROOT = Path(__file__).resolve().parents[1]
UPDATER = REPO_ROOT / "scripts" / "update.py"
DASHBOARD_JS = REPO_ROOT / "engraphis" / "static" / "dashboard.js"

# The private control plane's plan→feature table, mirrored from (read-only)
# engraphis-cloud/engraphis_cloud/entitlements.py PLAN_FEATURES. A drift here means a
# purchased capability silently renders as locked.
SERVER_PLAN_FEATURES = {
    "free": set(),
    "pro": {"analytics", "automation", "export", "sync"},
    "team": {"analytics", "automation", "export", "sync", "team"},
}
# Named separately by this client's commercial manifest; the server grants both under
# ``automation``, so any plan granting ``automation`` must grant these too.
CLIENT_AUTOMATION_ALIASES = {"consolidation", "dreaming"}


# ── (1) the updater must never hang on a stalled remote ───────────────────────
def test_the_updater_runs_no_unbounded_subprocess() -> None:
    """Every shell-out is routed through the one helper that always passes a budget."""

    tree = ast.parse(UPDATER.read_text(encoding="utf-8"))
    unbounded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = ast.unparse(node.func)
        if target in ("subprocess.run", "subprocess.call", "subprocess.check_output",
                      "subprocess.check_call", "subprocess.Popen"):
            if not any(kw.arg == "timeout" for kw in node.keywords):
                unbounded.append(target)

    assert unbounded == [], "unbounded subprocess call(s) in scripts/update.py: %s" % unbounded


def test_the_updater_helper_makes_a_timeout_impossible_to_omit() -> None:
    """``timeout`` has no default, so a new call site cannot silently be unbounded."""

    import scripts.update as updater

    timeout = inspect.signature(updater._run).parameters["timeout"]
    assert timeout.default is inspect.Parameter.empty

    tree = ast.parse(UPDATER.read_text(encoding="utf-8"))
    call_sites = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_run"
    ]
    assert call_sites, "expected the updater to shell out through _run"
    for node in call_sites:
        supplied = len(node.args) + len(node.keywords)
        assert supplied >= 3, ast.unparse(node)


def test_a_stalled_remote_aborts_instead_of_waiting_forever(monkeypatch) -> None:
    """The failing call is abandoned at its budget and reported, not awaited."""

    import scripts.update as updater

    seen = {}

    def _stall(cmd, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(updater.subprocess, "run", _stall)

    with pytest.raises(updater.UpdateTimeout) as excinfo:
        updater._remote_latest_tag("/usr/bin/git", "https://example.test/engraphis.git")

    assert isinstance(seen["timeout"], (int, float)) and seen["timeout"] > 0
    # A hang is only survivable if the user is told which step stalled and what to do.
    message = str(excinfo.value)
    assert "timed out" in message
    assert "engraphis-update" in message


def test_main_reports_a_stalled_step_and_exits(monkeypatch, capsys) -> None:
    """The stall must surface as a non-zero exit with copy, not an unhandled traceback."""

    import scripts.update as updater

    monkeypatch.setattr(updater, "_detect_install", lambda: "pypi")

    def _stall(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(updater.subprocess, "run", _stall)

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

    def _fake_run(cmd, **kwargs):
        cmd = list(cmd)
        calls.append(cmd)
        assert kwargs.get("timeout"), "every step must carry a budget: %s" % cmd

        def done(stdout=""):
            return subprocess.CompletedProcess(cmd, 0, stdout, "")

        if cmd[:4] == [sys.executable, "-m", "pip", "show"]:
            return done("Editable project location: %s\n" % project)
        if cmd[:5] == [sys.executable, "-m", "pip", "install", "-e"]:
            reinstalls["n"] += 1
            if reinstalls["n"] == 1:  # the upgrade reinstall stalls
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))
            return done()  # the rollback reinstall succeeds
        if "rev-parse" in cmd:
            return done("a" * 40 + "\n")
        if "symbolic-ref" in cmd:
            return done("main\n")
        if "ls-remote" in cmd:
            return done("%s\trefs/tags/v9.9.9\n" % ("b" * 40))
        if "rev-list" in cmd:
            return done("b" * 40 + "\n")
        if "status" in cmd:
            return done("")
        return done()

    monkeypatch.setattr(updater.subprocess, "run", _fake_run)

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
    assert SERVER_PLAN_FEATURES[plan].issubset(features)
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
    server_keys = set().union(*SERVER_PLAN_FEATURES.values())
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

    for field in ("plan", "features", "known_features", "is_trial", "trial",
                  "upgrade_url", "pro_upgrade_url", "team_upgrade_url"):
        assert field in payload, field
    assert "used" in payload["trial"]
    # Pro and Team bill through separate checkout targets.
    assert payload["pro_upgrade_url"] and payload["team_upgrade_url"]
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
