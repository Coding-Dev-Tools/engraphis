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
    assert "error.status===409" in script
    assert "Purchase Pro license" in script


# ── a paying customer must never be sold the plan they already own ────────────
# The hosted views route a failed request to one of three answers. Only an entitlement
# status may draw the purchase panel; a 409 is a conflict, and ``consent_required`` in
# particular is a customer who HAS paid and only has to set one environment variable.
#
# ``_route`` below executes the shipped routing rather than asserting on its source: the
# regression it guards (409 folded into ``hostedFeatureUnavailable``) kept every string
# these files already assert on, and only a run can tell which branch actually won.
_ROUTED_FUNCTIONS = (
    "hostedPlanUrl", "unlockHtml", "managedConsentHtml",
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
const LIC = {pro_upgrade_url:'https://engraphis.com/pricing',
             team_upgrade_url:'https://engraphis.com/pricing?plan=team',
             upgrade_url:'https://engraphis.com/pricing', trial:{used:false}};
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


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_consent_required_conflict_is_answered_with_a_purchase_panel(
    tmp_path, view,
):
    """Analytics and Automation are Pro sales surfaces, not error consoles."""

    rendered = _route(tmp_path, [{
        "name": "consent", "view": view,
        "error": {"status": 409, "detail": {"code": "consent_required"}},
    }])["consent"]

    assert 'class="upgrade-panel"' in rendered["html"]
    assert "Purchase Pro license" in rendered["html"]
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT=1" not in rendered["html"]
    assert rendered["pill"] == "PRO"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("status", [400, 401, 402, 500, 501])
def test_a_genuine_entitlement_failure_still_renders_the_upgrade_panel(
    tmp_path, view, status,
):
    """Every failed hosted request renders the Pro upgrade panel in these tabs."""

    rendered = _route(tmp_path, [{
        "name": "unentitled", "view": view, "error": {"status": status},
    }])["unentitled"]

    assert 'class="upgrade-panel"' in rendered["html"]
    assert "Purchase Pro license" in rendered["html"]
    assert "Start hosted Pro trial" in rendered["html"]
    assert rendered["pill"] == "PRO"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_an_internal_hosted_conflict_is_answered_with_a_purchase_panel(tmp_path, view):
    """Backend failures never expose internal messages in hosted tabs."""

    rendered = _route(tmp_path, [{
        "name": "conflict", "view": view,
        "message": "managed snapshot generation must advance",
        "error": {"status": 409, "detail": {"error": "generation conflict"}},
    }])["conflict"]

    assert 'class="upgrade-panel"' in rendered["html"]
    assert "Purchase Pro license" in rendered["html"]
    assert "managed snapshot generation must advance" not in rendered["html"]


def test_analytics_and_automation_suppress_hosted_error_details():
    """Only these sales surfaces redirect every failed hosted request to Pro."""

    helper = _dashboard_function("hostedFeatureUnavailable")
    assert "CURRENT_VIEW==='analytics'" in helper
    assert "CURRENT_VIEW==='automation'" in helper
    for entitlement_status in ("401", "402", "501"):
        assert entitlement_status in helper
    consent = _dashboard_function("managedConsentRequired")
    assert "CURRENT_VIEW!=='analytics'" in consent
    assert "CURRENT_VIEW!=='automation'" in consent

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
        "Signed compliance exports with bi-temporal checksums",
        "Priority support",
    ):
        assert benefit in script
    assert ".upgrade-panel" in styles


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
