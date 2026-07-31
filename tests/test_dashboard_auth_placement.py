"""Static UI contract for the single-user local client and hosted commercial boundary."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"
SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.js"
CLASSIC_SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "classic_assets" / "dashboard.js"
STYLES = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.css"


def test_dashboard_has_no_local_team_auth_or_license_activation_ui():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    for removed in ('id="session-action"', 'id="auth-overlay"', 'id="lic-key"'):
        assert removed not in html
    assert "activateLicense" not in script
    assert "'/license/activate'" not in script
    assert "Start ${TRIAL_DAYS}-day ${name} trial" in script
    assert "hostedCta('team','team_tab')" in script
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


def test_ledger_receipt_export_uses_authenticated_fetch_not_navigation():
    html = (Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets"
            / "index.html").read_text(encoding="utf-8")
    script = (Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets"
              / "ledger.js").read_text(encoding="utf-8")

    # Cookie-authenticated browser sessions require the bundle's custom request header.
    # A normal anchor cannot provide it, while ``api`` always does.
    assert '<button id="export-receipts"' in html
    assert 'href="/api/receipts/export"' not in html
    body = script[script.index("async function exportReceipts()"):
                  script.index("function switchManageTab")]
    assert "api(`/receipts/export?${query()}`)" in body
    assert "URL.createObjectURL(blob)" in body
    assert "byId('export-receipts').addEventListener('click', exportReceipts)" in script


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
    assert "Subscribe to ${name}" in script


def test_hosted_transfer_and_llm_consents_distinguish_sync_from_compute():
    """Cloud Sync is E2EE; compute and LLM consents identify transferred inputs."""

    legacy_scripts = (SCRIPT, CLASSIC_SCRIPT)
    for path in legacy_scripts:
        script = path.read_text(encoding="utf-8")
        assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in script
        assert "Engraphis Cloud cannot read their contents" in script
        assert "secret and session-scoped memories stay local" in script
        assert "uploads the selected workspace’s normal and sensitive memory content" in script
        assert "over HTTPS without end-to-end encryption" in script
        assert "Privacy, by design." not in script
        assert "configured LLM provider" in script
        assert "provider must read that text" in script
        assert "retention supervision is configured separately" in script
        assert "will never see, read, or access your data" not in script
        assert "confirmCloudTransfer('Save hosted policy'" in script
        assert "confirmCloudTransfer('Request hosted proposal'" in script
        assert "confirmCloudTransfer('Sync shared workspaces'" in script
        assert "Turn on LLM extraction" in script

    ledger = (Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets"
               / "ledger.js").read_text(encoding="utf-8")
    assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in ledger
    assert "Engraphis Cloud cannot read their contents" in ledger
    assert "secret and session-scoped memories stay local" in ledger
    assert "submits a bounded snapshot of this workspace’s normal and sensitive memory content" in ledger
    assert "configured LLM provider" in ledger
    assert "provider must read that text" in ledger
    assert "Retention supervision is ON" in ledger
    assert "bounded excerpt to the configured provider" in ledger
    assert "will never see, read, or access your data" not in ledger
    assert "Save this hosted policy" in ledger
    assert "Turn on LLM extraction" in ledger

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )
    normalized_readme = " ".join(readme.split())
    assert "hosted service must read it to produce a proposal" in normalized_readme
    assert "this is not end-to-end-encrypted processing" in normalized_readme
    assert "Local-only installations send nothing" in normalized_readme
    assert "ENGRAPHIS_RETENTION_SUPERVISOR=none" in normalized_readme

    assert "will never see, read, or access your data" not in normalized_readme

    sync_doc = (Path(__file__).resolve().parents[1] / "docs" / "SYNC.md").read_text(
        encoding="utf-8"
    )
    normalized_sync_doc = " ".join(sync_doc.split())
    assert "Local-only installations send no memory content" in normalized_sync_doc
    assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in normalized_sync_doc
    assert "Engraphis Cloud cannot read their contents" in normalized_sync_doc
    assert "Engraphis Cloud must process that snapshot to produce results" in normalized_sync_doc
    assert "hosted service can read that submitted content" not in normalized_sync_doc
    assert "Engraphis does not claim end-to-end encryption" not in normalized_sync_doc
    assert "will never see, read, or access it" not in normalized_sync_doc

    hosting_doc = (Path(__file__).resolve().parents[1] / "docs" / "HOSTING_RAILWAY.md").read_text(
        encoding="utf-8"
    )
    security_doc = (Path(__file__).resolve().parents[1] / "SECURITY.md").read_text(
        encoding="utf-8"
    )
    for document in (hosting_doc, security_doc):
        normalized = " ".join(document.split())
        assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in normalized
        assert "Engraphis Cloud cannot read their contents" in normalized
        assert "Managed compute is separate" in normalized


# ── a paying customer must never be sold the plan they already own ────────────
# The hosted views route a failed request to one of three answers. A 409 is a conflict,
# and ``consent_required`` means hosted work has not reached the installation yet. The consent
# panel can explain Pro to a local customer, while an existing subscriber sees their included
# feature rather than a second purchase or setup prompt.
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
    "withCtaAttribution", "hostedAccountUrl", "hostedPlanUrl", "hostedCta", "ctaLinkHtml",
    "unlockHtml", "managedConsentHtml",
    "managedConsentRequired", "cloudTrialSignupRequired", "hostedFeatureUnavailable",
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
const LIC_BASE = {pro_upgrade_url:'https://engraphis.com/pricing',
             team_upgrade_url:'https://engraphis.com/pricing?plan=team',
             upgrade_url:'https://engraphis.com/pricing',
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


def _dashboard_function(name, script_path=SCRIPT):
    """Slice one top-level declaration out of the shipped dashboard bundle."""

    script = script_path.read_text(encoding="utf-8")
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


def _route(tmp_path, cases, script_path=SCRIPT):
    """Run the real hosted-view error routing over ``cases`` and return what it rendered."""

    bundle = "\n".join([
        _ROUTING_STUBS,
        "\n".join(_dashboard_function(name, script_path) for name in _ROUTED_FUNCTIONS),
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
def test_a_trial_eligible_local_installation_is_answered_with_the_consent_panel(
    tmp_path, view,
):
    """An unconnected local install gets an honest, value-led Pro opportunity."""

    rendered = _route(tmp_path, [{
        "name": "trial-eligible", "view": view,
        "error": {"status": 401, "detail": {"code": "cloud_unconfigured"}},
    }])["trial-eligible"]

    assert 'class="hosted-opportunity"' in rendered["html"]
    assert ("Let your memory improve after you log off." if view == "automation" else
            "See the memory your team is about to lose.") in rendered["html"]
    assert "Start 3-day Pro trial" in rendered["html"]
    assert "Annual Pro option" in rendered["html"]
    assert "Hosted insights and maintenance come on automatically" in rendered["html"]
    assert "Secret and session-scoped memories stay local." in rendered["html"]
    # Consent travels with the cloud account; the customer is never sent to edit .env.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in rendered["html"]
    assert rendered["pill"] == "CLOUD"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_consent_panel_sends_an_existing_subscriber_to_cloud_not_checkout(tmp_path, view):
    rendered = _route(tmp_path, [{
        "name": "subscriber", "view": view,
        "error": {"status": 409, "detail": {"code": "consent_required"}},
        "lic": {
            "plan": "pro", "access_state": "active",
            "trial": {"used": False, "active": False, "available": False, "ends_at": 0},
        },
    }])["subscriber"]

    assert "Open Engraphis Cloud" in rendered["html"]
    assert "Hosted insights and maintenance are on by default" in rendered["html"]
    assert "Purchase Pro license" not in rendered["html"]
    assert "Start 3-day Pro trial" not in rendered["html"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_classic_hosted_tabs_offer_the_local_trial_without_a_setup_step(tmp_path, view):
    """Classic keeps the automatic three-day Cloud entry, not a local setup workflow."""

    rendered = _route(tmp_path, [{
        "name": "classic-trial", "view": view,
        "error": {"status": 401, "detail": {"code": "cloud_unconfigured"}},
    }], script_path=CLASSIC_SCRIPT)["classic-trial"]

    html = rendered["html"]
    assert "Start 3-day Pro trial" in html
    assert "Hosted insights and maintenance come on automatically" in html
    assert "no settings, toggles, or worker setup" in html
    # The two Cloud links are the complete unconnected path: trial or purchase. The
    # Classic tab must not add a local button for connecting, enabling, or configuring.
    assert html.count("<a ") == 2
    assert "<button" not in html
    assert "Connect this installation" not in html
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
    assert "Start 3-day Pro trial" in rendered["html"]
    assert "Subscribe to Pro" not in rendered["html"]
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
    expected_action = {
        "trial": "Open Engraphis Cloud",
        "trial_expired": "Subscribe to Pro",
        "lapsed": "Update billing",
        "active": "Open Engraphis Cloud",
    }[state]
    assert expected_action in rendered["html"]
    # And the one thing that must not.
    assert "Start 3-day Pro trial" not in rendered["html"]
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


# ── the license panel's own actions, executed rather than grepped ─────────────
# ``licActionsHtml`` is the second surface that turns an access state into a call to
# action, and the one a lapsed customer actually clicks. Running it is the only way to see
# which URL each button really carries.
_ACTION_FUNCTIONS = (
    "licAccessState", "licAccessLive", "licTrialAvailable", "licPlanName", "licPlanKey",
    "licTrialEnds", "fmtDay", "lockReason", "teamTeaserNote",
    "withCtaAttribution", "hostedAccountUrl", "hostedPlanUrl", "hostedCta", "ctaLinkHtml",
    "licActionsHtml",
)

_ACTION_STUBS = """
'use strict';
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g, c=>(
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function safeUrl(u){return (u && typeof u === 'string') ? u : '#'}
const TRIAL_DAYS = 3;
// Three distinct hosted targets, so a button carrying the wrong one is visible rather
// than hidden behind a shared URL.
// ``upgrade_url`` is deliberately the Pro checkout here: that is what
// ``licensing.upgrade_url()`` resolves to whenever ENGRAPHIS_PRO_UPGRADE_URL is set, so a
// portal button reading it instead of ``account_url`` is visible as a wrong URL.
const LIC_BASE = {pro_upgrade_url:'https://engraphis.example/checkout/pro',
                  team_upgrade_url:'https://engraphis.example/checkout/team',
                  upgrade_url:'https://engraphis.example/checkout/pro',
                  account_url:'https://engraphis.example/account',
                  plan:'local', access_state:'inactive',
                  trial:{used:false, active:false, available:false, ends_at:0}};
let LIC = LIC_BASE;
const location = {href:'https://127.0.0.1:8700/'};
"""

_ACTION_DRIVER = """
const CASES = JSON.parse(process.argv[2]);
const out = [];
for (const c of CASES) {
  LIC = Object.assign({}, LIC_BASE, c.lic || {});
  // Exactly how renderLicense and loadTeam call them.
  out.push({name: c.name, html: licActionsHtml(licAccessState()),
            teamNote: teamTeaserNote()});
}
process.stdout.write(JSON.stringify(out));
"""


def _actions(tmp_path, cases):
    """Run the shipped license-panel actions over ``cases`` and return what they rendered."""

    bundle = "\n".join([
        _ACTION_STUBS,
        "\n".join(_dashboard_function(name) for name in _ACTION_FUNCTIONS),
        _ACTION_DRIVER,
    ])
    runner = tmp_path / "lic_actions.js"
    runner.write_text(bundle, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), json.dumps(cases)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return {row["name"]: row for row in json.loads(result.stdout)}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("plan", ["team", "pro"])
def test_a_lapsed_customer_uses_the_plan_neutral_account_portal(tmp_path, plan):
    """Billing recovery must never be reframed as a new plan checkout."""

    html = _actions(tmp_path, [{
        "name": "lapsed", "lic": {"plan": plan, "access_state": "lapsed"},
    }])["lapsed"]["html"]

    assert html.count("Update billing") == 1
    assert html.count('href="https://engraphis.example/account?utm_source=engraphis') == 1
    assert "checkout/" not in html
    assert "?plan=" not in html
    # A lapsed customer is never offered a trial.
    assert "Start 3-day" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
def test_a_lapsed_customer_with_no_readable_plan_still_gets_a_billing_target(tmp_path):
    """``plan`` can be absent or unrecognised; the button must still go somewhere real."""

    html = _actions(tmp_path, [{
        "name": "lapsed", "lic": {"plan": "", "access_state": "lapsed"},
    }])["lapsed"]["html"]

    assert "Update billing" in html
    assert 'href="https://engraphis.example/account?utm_source=engraphis' in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("state,expected,absent", [
    # Only the state a trial can actually be started in draws the trial buttons; the
    # control plane refuses one for every organization that already holds an entitlement.
    ("inactive", "Start 3-day Pro trial", "Subscribe to Pro"),
    ("trial_expired", "Subscribe to Pro", "Start 3-day Pro trial"),
    ("trial", "Open Engraphis Cloud", "Start 3-day Pro trial"),
    ("active", "Open Engraphis Cloud", "Start 3-day Pro trial"),
])
def test_each_access_state_offers_the_one_action_that_can_succeed(
    tmp_path, state, expected, absent,
):
    html = _actions(tmp_path, [{
        "name": state,
        "lic": {"plan": "pro", "access_state": state,
                "trial": {"used": state != "inactive", "active": state == "trial",
                          "available": state == "inactive", "ends_at": 0}},
    }])[state]["html"]

    assert expected in html
    assert absent not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
def test_a_paying_team_customer_is_not_told_team_is_excluded(tmp_path):
    """The Team tab is a description of the hosted service, not an answer to a denial.

    It renders for every customer on every visit, so handing it ``lockReason(true)`` — the
    copy for a *refused* request — told a live Team subscriber "Your TEAM subscription does
    not include this" directly above an unlocked Team nav item and an Open Team Cloud
    button, while the backend plan table grants them the ``team`` feature.
    """

    rows = _actions(tmp_path, [
        {"name": "team-active", "lic": {"plan": "team", "access_state": "active"}},
        {"name": "team-trial", "lic": {
            "plan": "team", "access_state": "trial",
            "trial": {"used": True, "active": True, "available": False,
                      "ends_at": 1751068800}}},
        # Everyone the denial copy is actually about still gets it.
        {"name": "pro-active", "lic": {"plan": "pro", "access_state": "active"}},
        {"name": "team-lapsed", "lic": {"plan": "team", "access_state": "lapsed"}},
        {"name": "team-expired", "lic": {"plan": "team", "access_state": "trial_expired"}},
        {"name": "free", "lic": {"plan": "local", "access_state": "inactive",
                                 "trial": {"used": False, "active": False,
                                           "available": True, "ends_at": 0}}},
    ])

    assert rows["team-active"]["teamNote"] == (
        "Your TEAM subscription includes this. Organizations, roles, and seats are "
        "managed in Engraphis Cloud."
    )
    assert rows["team-trial"]["teamNote"] == (
        "Your free trial includes Team until 2025-06-28. Organizations, roles, and seats "
        "are managed in Engraphis Cloud."
    )
    for name in ("team-active", "team-trial"):
        assert "does not include" not in rows[name]["teamNote"], name

    # A Pro subscriber genuinely does not have Team, and a plan that is no longer live
    # grants nothing — both keep the accurate denial sentence.
    assert rows["pro-active"]["teamNote"] == "Your PRO subscription does not include this."
    assert "no longer active" in rows["team-lapsed"]["teamNote"]
    assert "free trial has ended" in rows["team-expired"]["teamNote"]
    assert "exactly 3 active days" in rows["free"]["teamNote"]


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


def test_pro_upgrade_panel_lists_every_pro_benefit_and_state_specific_cta():
    script = SCRIPT.read_text(encoding="utf-8")
    styles = STYLES.read_text(encoding="utf-8")

    assert 'class="upgrade-panel"' in script
    assert "Start ${TRIAL_DAYS}-day ${name} trial" in script
    assert "Subscribe to ${name}" in script
    for benefit in (
        "Hosted Cloud Sync across your installations",
        "Growth, retention, decay, and entity Analytics",
        "Auto Consolidation with hosted retention policies",
        "Auto Dreaming with reviewable managed proposals",
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
