"""Static UI contract for the single-user local client and hosted commercial boundary."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"
SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.js"
STYLES = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.css"


def test_dashboard_has_no_local_team_auth_or_license_activation_ui():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    for removed in ('id="session-action"', 'id="auth-overlay"', 'id="lic-key"'):
        assert removed not in html
    assert "activateLicense" not in script
    assert "'/license/activate'" not in script
    assert "Start hosted Pro trial" in script
    assert "Start hosted Team trial" in script
    # ``plan: local`` is the free customer runtime, not a paid local plan.
    assert "raw==='pro'||raw==='team'" in script
    assert "d.plan&&d.plan!=='free'" not in script


def test_failed_memory_open_cannot_save_against_a_stale_memory():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    body = script[script.index("async function openMem(id)"):
                  script.index("function closeMem()")]

    # Clear the prior identity and every write action before the detail request starts.
    assert body.index("window.CURMEM=null") < body.index("await api('/memory/")
    assert body.index("setEditorActionsEnabled(false)") < body.index("await api('/memory/")
    assert "setEditorActionsEnabled(true);return true" in body
    assert body.count("return false") >= 2

    wrapper = script[script.index("openMem=async function(id)"):
                     script.index("const selectViewWithDirtyGuard")]
    assert "if(loaded)editorCommitBaseline()" in wrapper
    assert "else{EDITOR_BASELINE='';editorRefreshDirty()}" in wrapper
    for control in ("ed-save-btn", "ed-pin-btn", "ed-forget-btn"):
        assert f'id="{control}"' in html


def test_first_boot_is_local_and_commercial_actions_open_hosted_cloud():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "renderHostedBootstrap" not in script
    assert "showHostedBootstrap" not in script
    assert "ENGRAPHIS_DEPLOYMENT_TOKEN" not in script
    assert "startTrialPlan" in script
    assert "Hosted signup URL is not configured" in script
    assert "Local API token required" in script
    assert "'/auth/state'" in script


def test_hosted_views_delegate_entitlement_to_cloud_proxy_responses():
    script = SCRIPT.read_text(encoding="utf-8")
    analytics_view = script[script.index("function loadAnalyticsView()"):
                            script.index("function loadAutomationView()")]
    automation_view = script[script.index("function loadAutomationView()"):
                             script.index("function workspaceRequired")]
    assert "return loadAnalytics()" in analytics_view
    assert "return loadAutomation()" in automation_view
    assert "LIC.features" not in analytics_view + automation_view

    analytics = script[script.index("async function loadAnalytics()"):
                       script.index("/* ── hosted automation policy")]
    automation = script[script.index("async function loadAutomation()"):
                        script.index("async function saveAutomation()")]
    for body in (analytics, automation):
        assert "hostedFeatureUnavailable(e)" in body
        assert "unlockHtml" in body
    # The billing predicate itself, verbatim: the loaders delegate to it, so widening it
    # here is the only way a non-billing failure can reach ``unlockHtml``.
    assert "error.status===402||error.status===501" in script
    assert "error.status===409" in script
    assert "Purchase Pro license" in script


# ── a paying customer must never be sold the plan they already own ────────────
# The hosted views route a failed request to one of three answers. Only an entitlement
# status may draw the purchase panel; a 409 is a conflict, and ``consent_required`` in
# particular is a customer who HAS paid and whose installation simply has managed compute
# turned off — connecting it to Engraphis Cloud is what turns it on.
#
# ``_route`` below executes the shipped routing rather than asserting on its source: the
# regression it guards (409 folded into ``hostedFeatureUnavailable``) kept every string
# these files already assert on, and only a run can tell which branch actually won.
_ROUTED_FUNCTIONS = (
    # The access-state readers the panel copy is now derived from. They are bundled as the
    # real shipped functions rather than stubbed, so "does this customer get offered a
    # trial" is answered here by the code that answers it in the browser.
    "licAccessState", "licAccessLive", "licTrialActive", "licTrialAvailable",
    "licPlanName", "licPlanKey", "licTrialEnds", "fmtDay", "lockReason",
    "hostedAccountUrl", "hostedPlanUrl", "licActionsHtml", "unlockHtml",
    "managedConsentHtml",
    "managedConsentRequired", "hostedFeatureUnavailable",
    "loadAnalytics", "loadAutomation",
)

# Everything the routed code touches that is not itself under test. The DOM, the API call
# and the license blob are stubbed; ``esc`` is the real escaping contract.
_ROUTING_STUBS = """
'use strict';
const NODES = {};
const document = {getElementById(id){
  return NODES[id] || (NODES[id] = {innerHTML:'', textContent:'', className:'', style:{}})}};
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g, c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function safeUrl(u){return u || '#'}
function setPlanPill(el,text,cls){if(el){el.textContent=text;el.className=cls}}
function showAs(el,on,disp){if(el)el.style.display=on?(disp||'block'):'none'}
function renderAnalytics(){return '<div id="rendered-analytics"></div>'}
function fmtRel(){return 'just now'}
function toast(){}
const TRIAL_DAYS = 3, WS = 'workspace';
let CURRENT_VIEW = 'overview';
// The default is an unconnected installation: no hosted plan, unspent trial, and the
// control plane says a trial may still be started. A case can replace ``access_state`` and
// ``trial`` to model a trialist, a spent trial, a paying customer, or a lapsed one.
const LIC_BASE = {pro_upgrade_url:'https://engraphis.example/checkout/pro',
             team_upgrade_url:'https://engraphis.example/checkout/team',
             upgrade_url:'https://engraphis.example/account',
             plan:'local', access_state:'inactive',
             trial:{used:false, active:false, available:true, ends_at:0}};
let LIC = LIC_BASE;
const location = {href:'https://127.0.0.1:8077/'};
let THROWN = null;
async function api(){if(THROWN) throw THROWN; return {}}
"""

_ROUTING_DRIVER = """
const CASES = JSON.parse(process.argv[2]);
(async () => {
  const out = [];
  for (const c of CASES) {
    THROWN = Object.assign(new Error(c.message || 'request failed'), c.error);
    LIC = Object.assign({}, LIC_BASE, c.lic || {});
    CURRENT_VIEW = c.view;
    for (const key of Object.keys(NODES)) delete NODES[key];
    await (c.view === 'analytics' ? loadAnalytics() : loadAutomation());
    const body = c.view === 'analytics' ? 'analytics-body' : 'automation-body';
    const lock = c.view === 'analytics' ? 'an-lock' : 'au-lock';
    out.push({name: c.name, html: NODES[body].innerHTML, pill: NODES[lock].textContent});
  }
  process.stdout.write(JSON.stringify(out));
})().catch(err => {process.stderr.write(String(err && err.stack)); process.exit(1)});
"""


def _dashboard_function(name):
    """Slice one top-level declaration out of the shipped dashboard bundle."""

    script = SCRIPT.read_text(encoding="utf-8")
    for head in ("\nasync function %s(" % name, "\nfunction %s(" % name):
        start = script.find(head)
        if start >= 0:
            start += 1
            break
    else:
        raise AssertionError("dashboard.js no longer declares %s" % name)
    end = re.compile(r"\n(?:async function |function |/\* |// |const |let |var )").search(
        script, start)
    return script[start:end.start() if end else len(script)].rstrip()


def _route(tmp_path, cases):
    """Run the real hosted-view error routing over ``cases`` and return what it rendered."""

    bundle = "\n".join([
        _ROUTING_STUBS,
        "\n".join(_dashboard_function(name) for name in _ROUTED_FUNCTIONS),
        _ROUTING_DRIVER,
    ])
    runner = tmp_path / "routing.js"
    runner.write_text(bundle, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), json.dumps(cases)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {row["name"]: row for row in json.loads(result.stdout)}


def _license_copy(tmp_path, cases):
    """Execute the shipped entitlement copy and action helpers under Node."""

    functions = (
        "licAccessState", "licAccessLive", "licTrialAvailable", "licPlanName", "licPlanKey",
        "licTrialEnds", "fmtDay", "hostedAccountUrl", "hostedPlanUrl",
        "lockReason", "teamTeaserNote", "licActionsHtml",
    )
    driver = """
const CASES = JSON.parse(process.argv[2]);
const out = [];
for (const c of CASES) {
  LIC = Object.assign({}, LIC_BASE, c.lic);
  out.push({
    name: c.name,
    reason: lockReason(true),
    teamNote: teamTeaserNote(),
    actions: licActionsHtml(c.state)
  });
}
process.stdout.write(JSON.stringify(out));
"""
    runner = tmp_path / "license-copy.js"
    runner.write_text("\n".join([
        _ROUTING_STUBS,
        "\n".join(_dashboard_function(name) for name in functions),
        driver,
    ]), encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), json.dumps(cases)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {row["name"]: row for row in json.loads(result.stdout)}


def test_lock_reason_has_one_plain_text_escaping_contract() -> None:
    """The helper returns text; each shipped HTML sink owns exactly one escape."""

    script = SCRIPT.read_text(encoding="utf-8")
    reason = _dashboard_function("lockReason")
    upgrade = _dashboard_function("unlockHtml")

    assert "esc(" not in reason
    assert "${esc(detail)}" in upgrade
    assert "esc(lockReason(false))" in script
    assert "esc(teamTeaserNote())" in script


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
def test_team_entitlements_use_truthful_copy_and_the_team_billing_url(tmp_path) -> None:
    rendered = _license_copy(tmp_path, [
        {
            "name": "active-team",
            "state": "active",
            "lic": {
                "plan": "team", "access_state": "active",
                "trial": {"available": False, "ends_at": 0},
            },
        },
        {
            "name": "team-trial",
            "state": "trial",
            "lic": {
                "plan": "team", "access_state": "trial",
                "trial": {"available": False, "ends_at": 1_800_000_000},
            },
        },
        {
            "name": "lapsed-team",
            "state": "lapsed",
            "lic": {
                "plan": "team", "access_state": "lapsed",
                "trial": {"available": False, "ends_at": 0},
            },
        },
    ])

    assert "runs in Engraphis Team Cloud" in rendered["active-team"]["reason"]
    assert "does not include" not in rendered["active-team"]["reason"]
    assert "Your Team trial is live" in rendered["team-trial"]["reason"]
    assert "needs a subscription" not in rendered["team-trial"]["reason"]
    assert "Your TEAM subscription includes this" in rendered["active-team"]["teamNote"]
    assert "does not include" not in rendered["active-team"]["teamNote"]
    assert "Your free trial includes Team" in rendered["team-trial"]["teamNote"]
    assert "Update billing" in rendered["lapsed-team"]["actions"]
    assert "plan=team" in rendered["lapsed-team"]["actions"]
    assert "plan=pro" not in rendered["lapsed-team"]["actions"]
    assert 'href="https://engraphis.example/account"' in rendered["lapsed-team"]["actions"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("plan,mine,theirs", [
    ("team", "team", "pro"),
    ("pro", "pro", "team"),
])
def test_a_lapsed_customer_renews_the_plan_they_actually_hold(
    tmp_path, plan, mine, theirs,
) -> None:
    actions = _license_copy(tmp_path, [{
        "name": "lapsed",
        "state": "lapsed",
        "lic": {
            "plan": plan,
            "access_state": "lapsed",
            "trial": {"available": False, "ends_at": 0},
        },
    }])["lapsed"]["actions"]

    assert "Update billing" in actions and "Open account portal" in actions
    assert "checkout/%s?plan=%s" % (mine, mine) in actions
    assert "checkout/%s" % theirs not in actions
    assert 'href="https://engraphis.example/account"' in actions
    assert "Start hosted" not in actions


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
def test_a_lapsed_customer_with_no_readable_plan_still_gets_a_billing_target(
    tmp_path,
) -> None:
    actions = _license_copy(tmp_path, [{
        "name": "lapsed",
        "state": "lapsed",
        "lic": {
            "plan": "",
            "access_state": "lapsed",
            "trial": {"available": False, "ends_at": 0},
        },
    }])["lapsed"]["actions"]

    assert "Update billing" in actions
    assert "checkout/pro?plan=pro" in actions


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("state,expected,absent", [
    ("inactive", "Start hosted Pro trial", "Subscribe to Pro"),
    ("trial_expired", "Subscribe to Pro", "Start hosted Pro trial"),
    ("trial", "Open Pro Cloud", "Start hosted Pro trial"),
    ("active", "Open Pro Cloud", "Start hosted Pro trial"),
])
def test_each_access_state_offers_the_one_action_that_can_succeed(
    tmp_path, state, expected, absent,
) -> None:
    actions = _license_copy(tmp_path, [{
        "name": state,
        "state": state,
        "lic": {
            "plan": "pro",
            "access_state": state,
            "trial": {
                "used": state != "inactive",
                "active": state == "trial",
                "available": state == "inactive",
                "ends_at": 0,
            },
        },
    }])[state]["actions"]

    assert expected in actions
    assert absent not in actions


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_consent_required_conflict_is_answered_with_the_consent_panel(
    tmp_path, view,
):
    """A 409 ``consent_required`` is a conflict, not a bill — never sell Pro for it.

    Analytics and Automation are the two views that reach managed compute, so they are
    exactly the views this panel has to appear in.
    """

    rendered = _route(tmp_path, [{
        "name": "consent", "view": view,
        "error": {"status": 409, "detail": {"code": "consent_required"}},
    }])["consent"]

    assert "managed compute is turned off for this installation" in rendered["html"]
    assert "Connect this installation to Engraphis Cloud" in rendered["html"]
    assert 'class="upgrade-panel"' not in rendered["html"]
    assert "Purchase Pro license" not in rendered["html"]
    # Consent travels with the cloud account; the customer is never sent to edit ``.env``.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in rendered["html"]
    assert rendered["pill"] == "CLOUD"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("status", [402, 501])
def test_a_genuine_entitlement_failure_still_renders_the_upgrade_panel(
    tmp_path, view, status,
):
    """402 not subscribed and 501 not offered are billing answers. 401 is not:

    the cloud maps it to "connect again", so it must reach the customer as a reconnect
    instruction rather than a panel selling a plan they may already own.
    """

    rendered = _route(tmp_path, [{
        "name": "unentitled", "view": view, "error": {"status": status},
    }])["unentitled"]

    assert 'class="upgrade-panel"' in rendered["html"]
    assert "Purchase Pro license" in rendered["html"]
    assert "Start hosted Pro trial" in rendered["html"]
    assert rendered["pill"] == "PRO"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("state,reason", [
    ("trial", "Your free trial is live"),
    ("trial_expired", "Your free trial has ended"),
    ("lapsed", "no longer active"),
    ("active", "does not include this"),
])
def test_the_upgrade_panel_never_offers_a_trial_the_server_would_refuse(
    tmp_path, view, state, reason,
):
    """``start_trial`` refuses every organization that already holds an entitlement.

    ``trial.used`` was hardcoded false by ``/api/license``, so this panel offered "Start
    hosted Pro trial" to every connected customer forever — a trialist mid-trial, a
    customer whose trial had already been spent, and an active subscriber alike. All three
    got a 409 from the control plane for clicking it. The panel now says which of those
    four situations the customer is actually in, and only sells what is buyable.
    """

    rendered = _route(tmp_path, [{
        "name": "gated", "view": view, "error": {"status": 402},
        "lic": {
            "plan": "pro", "access_state": state,
            "trial": {"used": state != "active", "active": state == "trial",
                      "available": False, "ends_at": 1785240000},
        },
    }])["gated"]

    assert 'class="upgrade-panel"' in rendered["html"]
    # The one thing that must always still be offered.
    assert "Purchase Pro license" in rendered["html"]
    # And the one thing that must not.
    assert "Start hosted Pro trial" not in rendered["html"]
    assert reason in rendered["html"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_a_non_billing_failure_shows_the_error_instead_of_selling_pro(
    tmp_path, view, status,
):
    """A bad request, an expired session, or a cloud outage is not an unpaid invoice.

    401 belongs here rather than with 402/501: the cloud maps it to "the cloud session
    expired or was revoked; connect again". Drawing the purchase panel for it sold an
    already-paying customer the plan they own, instead of telling them to reconnect.
    """

    rendered = _route(tmp_path, [{
        "name": "broken", "view": view,
        "message": "the hosted service is briefly unavailable",
        "error": {"status": status},
    }])["broken"]

    assert 'class="upgrade-panel"' not in rendered["html"]
    assert "Purchase Pro license" not in rendered["html"]
    assert "the hosted service is briefly unavailable" in rendered["html"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_transient_hosted_conflict_is_not_answered_with_a_purchase_panel(
    tmp_path, view,
):
    """A 409 that is not ``consent_required`` is a state conflict, not a billing answer."""

    rendered = _route(tmp_path, [{
        "name": "conflict", "view": view,
        "message": "managed snapshot generation must advance",
        "error": {"status": 409, "detail": {"error": "generation conflict"}},
    }])["conflict"]

    assert 'class="upgrade-panel"' not in rendered["html"]
    assert "Purchase Pro license" not in rendered["html"]
    # The customer sees the real cause and can retry, rather than a panel selling Pro.
    assert "managed snapshot generation must advance" in rendered["html"]


def test_only_an_entitlement_status_may_draw_the_purchase_panel():
    """Pin both routing predicates literally.

    The regression these guard folded a view name into ``hostedFeatureUnavailable`` and out
    of ``managedConsentRequired``, which made every failure on Analytics and Automation --
    including a 409 from a paying customer -- render the panel selling Pro. A loose
    substring check passed straight through that, so assert the exact predicate and the
    exhaustive set of statuses it may test.
    """

    helper = _dashboard_function("hostedFeatureUnavailable")
    assert "error.status===402||error.status===501" in helper
    # No view may widen it and no fourth status may join it.
    assert "CURRENT_VIEW" not in helper
    assert sorted(set(re.findall(r"status\s*===\s*(\d+)", helper))) == ["402", "501"]

    consent = _dashboard_function("managedConsentRequired")
    assert "error.status===409" in consent
    assert "code==='consent_required'" in consent
    assert sorted(set(re.findall(r"status\s*===\s*(\d+)", consent))) == ["409"]
    # The consent branch must fire in every view, including the two sales surfaces.
    assert "CURRENT_VIEW" not in consent

    for name in (
        "loadOverviewAnalytics",
        "loadAnalytics",
        "loadAutomation",
        "loadSyncStatus",
    ):
        body = _dashboard_function(name)
        assert "managedConsentRequired(e)" in body, name
        assert "managedConsentHtml(" in body, name
        assert body.index("managedConsentRequired(e)") < body.index(
            "hostedFeatureUnavailable(e)"), name


def test_sync_status_does_not_sell_pro_to_a_customer_who_already_owns_it():
    """``loadSyncStatus`` rendered the purchase panel for EVERY failure.

    A dropped connection or a 5xx from the relay told a paying Pro customer to buy Pro.
    Only a billing answer may reach ``unlockHtml``; everything else shows the real error.
    """

    body = _dashboard_function("loadSyncStatus")

    assert "unlockHtml('Cloud Sync','pro')" in body
    # The unlock must be reached through the entitlement predicate, never unconditionally.
    assert "hostedFeatureUnavailable(e)" in body
    assert body.index("hostedFeatureUnavailable(e)") < body.index("unlockHtml(")
    # A cause that is neither consent nor billing surfaces the server's own message.
    assert "esc(e.message)" in body


def test_pro_upgrade_panel_lists_every_pro_benefit_and_purchase_cta():
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'class="upgrade-panel"' in script
    assert "Start hosted Pro trial" in script
    assert "Purchase Pro license" in script
    for benefit in (
        "Hosted Cloud Sync across your installations",
        "Growth, retention, decay, and entity Analytics",
        "Auto Consolidation with hosted retention policies",
        "Auto Dreaming with reviewable managed proposals",
        "Priority support",
    ):
        assert benefit in script
    assert ".upgrade-panel" in styles
    # Regression: the panel sold "Signed compliance exports with bi-temporal checksums"
    # while no signing code existed in this client and Engraphis Cloud had no export
    # route, scope, or job kind. Do not re-add it without a capability behind it.
    assert "compliance export" not in script.lower()


def test_team_invitations_and_password_setup_are_not_in_local_client():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for removed in (
        "getInvitationToken", "showInvitationForm", "Accept team invitation",
        "Confirm password", "'/auth/invitations/accept'", "invite_token", "reset_token",
    ):
        assert removed not in script
    assert 'id="auth-overlay"' not in html
    assert "Organizations, invitations, roles, named seats" in script
    assert "private hosted service" in script


def test_untrusted_values_are_not_spliced_into_inline_javascript_literals():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    handlers = "\n".join(re.findall(
        r'h\d+:function\(event\)\{([^\n]*)\},', script,
    ))

    # HTML escaping does not make a value safe inside the single-quoted JavaScript
    # literal used by an inline handler: character references decode before execution.
    # Carry untrusted identifiers in data-* attributes and read them from ``this``.
    for interpolation in (
        "${esc(m.id)}", "${esc(w.name)}", "${esc(u.id)}",
        "${esc(u.email)}", "${t.id}",
    ):
        assert interpolation not in handlers
    assert "openMem(this.dataset.id)" in handlers
    assert "folderCardName(this)" in handlers
    assert " onclick=" not in html
