"""UI contract for the single-user local client and its hosted commercial boundary.

Ledger is the only dashboard. Its commercial copy is executed rather than grepped wherever a
regression could keep every string in place while changing which branch wins: a paying
customer must never be sold the plan they already own, a lapsed one must reach billing, and
an outage must read as an outage rather than an unpaid invoice.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "engraphis" / "dashboard_assets"
INDEX = ASSETS / "index.html"
SCRIPT = ASSETS / "ledger.js"
STYLES = ASSETS / "ledger.css"

requires_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required to run the UI")


def _ledger_declaration(name):
    """Slice one declaration out of ledger.js's top-level IIFE scope (two-space indent)."""

    script = SCRIPT.read_text(encoding="utf-8")
    match = re.search(
        r"^  (?:async function |function |const |let )" + re.escape(name) + r"\b",
        script, re.MULTILINE)
    assert match is not None, "ledger.js no longer declares %s" % name
    end = re.compile(r"^  (?:async function |function |const |let |var |/\*|//)",
                     re.MULTILINE).search(script, match.end())
    return script[match.start():end.start() if end else len(script)].rstrip()


def _section(script, start, end):
    return script[script.index(start):script.index(end, script.index(start))]


def test_dashboard_has_no_local_team_auth_or_license_activation_ui():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    for removed in ('id="session-action"', 'id="auth-overlay"', 'id="lic-key"'):
        assert removed not in html
    assert "activateLicense" not in script
    assert "/license/activate" not in script
    # ``plan: local`` is the free customer runtime, not a paid local plan.
    assert "return value === 'pro' || value === 'team' ? value : '';" in _ledger_declaration(
        "licensePlanKey")


def test_selecting_a_memory_closes_the_previous_editor_before_loading():
    """A failed or slow memory load must never leave an editor bound to the last memory."""

    html = INDEX.read_text(encoding="utf-8")
    body = _ledger_declaration("selectMemory")
    request = body.index("await api(`/memory/")

    # The prior identity and its editor are cleared before the detail request starts.
    assert body.index("resetEditorSession();") < request
    assert body.index("byId('memory-editor').hidden = true;") < request
    assert body.index("target.replaceChildren(empty('Loading memory…'));") < request
    # Edit actions exist only for the memory that actually arrived, and a stale or failed
    # answer returns before any of them is created.
    assert body.index("if (!memory || state.selectedMemory !== id || !isCurrentScopedRequest("
                      "request)) return;") < body.index("openEditor(memory)")
    assert request < body.index("openEditor(memory)")
    assert 'id="memory-editor"' in html


def test_first_boot_is_local_and_commercial_actions_open_hosted_cloud():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for removed in ("renderHostedBootstrap", "showHostedBootstrap", "ENGRAPHIS_DEPLOYMENT_TOKEN"):
        assert removed not in script
        assert removed not in html
    # A protected deployment asks for its token in the masked in-page dialog.
    assert "needs its API token before the dashboard can load" in script
    # Team state comes from the local auth surface; plans open hosted Cloud.
    assert "api('/auth/state')" in script
    assert "hostedCta(plan.id, 'plans', interval)" in _ledger_declaration("renderPlans")


def test_ledger_receipt_export_uses_authenticated_fetch_not_navigation():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    # Cookie-authenticated browser sessions require the bundle's custom request header.
    # A normal anchor cannot provide it, while ``api`` always does.
    assert '<button id="export-receipts"' in html
    assert 'href="/api/receipts/export"' not in html
    body = script[script.index("async function exportReceipts()"):
                  script.index("function switchManageTab")]
    assert "api(`/receipts/export?${query()}`)" in body
    assert "URL.createObjectURL(blob)" in body
    assert "byId('export-receipts').addEventListener('click', exportReceipts)" in script


def test_ledger_remote_token_entry_is_masked_and_cleared_between_attempts():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")

    assert 'id="browser-auth-dialog"' in html
    assert 'id="browser-auth-token" type="password"' in html
    assert 'autocomplete="off"' in html
    prompt = script[script.index("function promptBrowserToken("):
                    script.index("async function authenticateBrowser")]
    authenticate = script[script.index("async function authenticateBrowser"):
                          script.index("async function reviewCsrfToken")]
    assert "window.prompt(" not in prompt + authenticate
    assert "input.value = ''" in prompt
    assert "dialog.showModal()" in prompt
    assert "form.addEventListener('submit'" in prompt


def test_hosted_views_delegate_entitlement_to_cloud_proxy_responses():
    """The client never pre-judges entitlement: it asks Cloud and classifies the answer."""

    loader = _ledger_declaration("loadHosted")
    assert "features" not in loader
    assert "licenseHasHostedAccess" not in loader
    # Every refusal on a hosted tab goes through the one classifier.
    assert "renderAnalyticsStartError(target, workspace, error)" in loader
    assert "const failure = hostedFailure(kind, error);" in loader
    assert "hostedFailure('analytics', error)" in _ledger_declaration("renderAnalyticsStartError")
    script = SCRIPT.read_text(encoding="utf-8")
    for call in re.finditer(r"(?<!function )renderHostedFailure\((\w+), failure\)", script):
        preceding = script[max(0, call.start() - 240):call.start()]
        assert "hostedFailure(" in preceding, "a purchase panel was drawn without the classifier"


def test_hosted_transfer_and_llm_consents_distinguish_sync_from_compute():
    """Cloud Sync is E2EE; compute and LLM consents identify transferred inputs."""

    ledger = SCRIPT.read_text(encoding="utf-8")
    assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in ledger
    assert "Engraphis Cloud cannot read their contents" in ledger
    assert "secret and session-scoped memories stay local" in ledger
    assert "submits a bounded snapshot of this workspace’s normal and sensitive memory content" in ledger
    assert "configured LLM provider" in ledger
    assert "provider must read that text" in ledger
    assert "Retention supervision is ON" in ledger
    assert "bounded excerpt to the configured provider" in ledger
    assert "will never see, read, or access your data" not in ledger
    assert "Privacy, by design." not in ledger
    assert "Save this hosted policy" in ledger
    assert "Turn on LLM extraction" in ledger

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    normalized_readme = " ".join(readme.split())
    assert "hosted service must read it to produce a proposal" not in normalized_readme
    assert "this is not end-to-end-encrypted processing" not in normalized_readme
    assert "Local-only installations send nothing" not in normalized_readme
    assert "will never see, read, or access your data" not in normalized_readme

    sync_doc = (ROOT / "docs" / "SYNC.md").read_text(encoding="utf-8")
    normalized_sync_doc = " ".join(sync_doc.split())
    assert "Local-only installations send no memory content" in normalized_sync_doc
    assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in normalized_sync_doc
    assert "Engraphis Cloud cannot read their contents" in normalized_sync_doc
    assert "Engraphis Cloud must process that snapshot to produce results" in normalized_sync_doc
    assert "hosted service can read that submitted content" not in normalized_sync_doc
    assert "Engraphis does not claim end-to-end encryption" not in normalized_sync_doc
    assert "will never see, read, or access it" not in normalized_sync_doc

    hosting_doc = (ROOT / "docs" / "HOSTING_RAILWAY.md").read_text(encoding="utf-8")
    security_doc = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    for document in (hosting_doc, security_doc):
        normalized = " ".join(document.split())
        assert "Cloud Sync encrypts eligible shared-workspace changes end-to-end" in normalized
        assert "Engraphis Cloud cannot read their contents" in normalized
        assert "Managed compute is separate" in normalized


# ── the shipped commercial helpers, executed ──────────────────────────────────
# Each helper below is sliced verbatim out of ledger.js and run under Node against a DOM just
# large enough for Ledger's own ``node``/``button``/``empty`` helpers. Only the network, the
# view switcher and the clock-free date formatting are the harness's own.
_COMMERCIAL_DECLARATIONS = (
    "byId", "text", "empty", "node", "button", "validDate", "relative", "definitionList",
    "safeUrl",
    "licenseAccessState", "licensePlanKey", "licenseTrialAvailable", "licenseHasHostedAccess",
    "withCtaAttribution", "hostedPlanUrl", "hostedAccountUrl", "licenseTrialDays", "hostedCta",
    "lockReason", "hostedFailure", "renderHostedFailure", "renderAnalyticsStartError",
    "renderTeamState", "LICENSE_STATUS_NOTES", "LICENSE_SOURCES", "licenseTrialEnds",
    "licensePlanLabel", "renderLicenseStatus",
)

_HARNESS_PRELUDE = """
'use strict';
class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = '';
    this.ownText = '';
    this.listeners = {};
    this.hidden = false;
    this.href = '';
    this.target = '';
    this.rel = '';
    this.type = '';
  }
  set textContent(value) { this.ownText = String(value); this.children = []; }
  get textContent() {
    return this.ownText + this.children.map(child => child.textContent).join('');
  }
  append(...items) {
    items.filter(item => item != null).forEach(item => this.children.push(
      typeof item === 'string' ? document.createTextNode(item) : item));
  }
  replaceChildren(...items) { this.ownText = ''; this.children = []; this.append(...items); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  addEventListener(type, handler) { (this.listeners[type] = this.listeners[type] || []).push(handler); }
  click() { (this.listeners.click || []).forEach(handler => handler({ preventDefault() {} })); }
}
const document = {
  createElement: tag => new FakeElement(tag),
  createTextNode: value => ({ textContent: String(value), children: [] }),
  getElementById: () => null,
};
const location = { href: 'https://127.0.0.1:8700/' };
const state = { license: null };
const switched = [];
const switchManageTab = tab => switched.push(tab);
const runFreshAnalytics = () => switched.push('fresh-analytics');
const describe = element => {
  const out = [];
  const walk = item => {
    if (!item || !item.tagName) return;
    if (item.tagName === 'A') out.push({ link: item.textContent, href: item.href });
    if (item.tagName === 'BUTTON') out.push({ button: item.textContent });
    item.children.forEach(walk);
  };
  walk(element);
  return { text: element.textContent, actions: out };
};
"""

_HARNESS_DRIVER = """
const LICENSE_BASE = {
  plan: 'local', plan_source: 'local', access_state: 'inactive',
  upgrade_url: 'https://engraphis.example/checkout/pro',
  pro_upgrade_url: 'https://engraphis.example/checkout/pro',
  team_upgrade_url: 'https://engraphis.example/checkout/team',
  account_url: 'https://engraphis.example/account',
  trial: { used: false, active: false, available: true, ends_at: 0,
           trial_days: 7, days_by_plan: { pro: 7, team: 14 } },
};
const out = {};
for (const item of JSON.parse(process.argv[2])) {
  state.license = { ...LICENSE_BASE, ...(item.license || {}) };
  switched.length = 0;
  const target = document.createElement('div');
  if (item.kind === 'failure') {
    const error = Object.assign(new Error(item.message || 'request failed'), item.error || {});
    const failure = hostedFailure(item.view, error);
    if (failure) renderHostedFailure(target, failure);
    else if (item.view === 'analytics') renderAnalyticsStartError(target, 'acme', error);
    else target.replaceChildren(empty(`Automation is not active: ${error.message}`));
    const rendered = describe(target);
    target.children.forEach(child => child.children && child.children.forEach(
      control => control.tagName === 'BUTTON' && control.click()));
    out[item.name] = { failure, ...rendered, switched: [...switched] };
  } else if (item.kind === 'cta') {
    out[item.name] = hostedCta(item.plan || 'pro', item.content || 'license');
  } else if (item.kind === 'team') {
    renderTeamState(target, { deployment_mode: 'local', enabled: false }, state.license);
    out[item.name] = describe(target);
  } else if (item.kind === 'status') {
    const fake = document.createElement('div');
    document.getElementById = id => (id === 'license-status-body' ? fake : null);
    renderLicenseStatus(state.license);
    out[item.name] = describe(fake);
  }
}
process.stdout.write(JSON.stringify(out));
"""


def _run_commercial(tmp_path, cases):
    """Execute the shipped Ledger helpers over ``cases`` and return what they produced."""

    bundle = "\n".join([
        _HARNESS_PRELUDE,
        "\n".join(_ledger_declaration(name) for name in _COMMERCIAL_DECLARATIONS),
        _HARNESS_DRIVER,
    ])
    runner = tmp_path / "commercial.js"
    runner.write_text(bundle, encoding="utf-8")
    result = subprocess.run(
        ["node", str(runner), json.dumps(cases)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _labels(rendered):
    return [action.get("button") or action.get("link") for action in rendered["actions"]]


FEATURES = {"analytics": "Analytics", "automation": "Hosted automation",
            "sync": "Cloud Sync"}


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_trial_eligible_local_installation_is_offered_the_trial_and_approval_steps(
    tmp_path, view,
):
    """An unconnected local install hears what Pro does and how processing is approved."""

    rendered = _run_commercial(tmp_path, [{
        "name": "trial-eligible", "kind": "failure", "view": view,
        "error": {"status": 401, "code": "cloud_unconfigured"},
    }])["trial-eligible"]

    assert rendered["failure"]["action"] == "plan"
    assert rendered["failure"]["title"] == "%s runs in Engraphis Cloud" % FEATURES[view]
    assert "An email-confirmed, no-card Pro trial lasts exactly 7 active days." in rendered["text"]
    assert "approve each workspace in Settings → Preferences" in rendered["text"]
    # Workspace approval is separate from Cloud access; no environment edit is needed.
    assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in rendered["text"]
    assert _labels(rendered) == ["Compare plans"]
    assert rendered["switched"] == ["plans"]


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_consent_panel_links_subscribers_to_workspace_approval_without_checkout(tmp_path, view):
    rendered = _run_commercial(tmp_path, [{
        "name": "subscriber", "kind": "failure", "view": view,
        "error": {"status": 409, "code": "consent_required"},
        "license": {"plan": "pro", "access_state": "active", "plan_source": "cloud",
                    "trial": {"used": False, "active": False, "available": False}},
    }])["subscriber"]

    assert rendered["failure"]["action"] == "consent"
    assert ("Your plan includes %s. Readable managed processing is paused until you approve "
            "this workspace in Settings → Preferences." % FEATURES[view]) in rendered["text"]
    assert "Secret and session-scoped memories stay local." in rendered["text"]
    assert _labels(rendered) == ["Review workspace processing"]
    # The button opens the Preferences tab that holds the managed-processing approval.
    assert rendered["switched"] == ["settings"]
    for sale in ("Subscribe", "Start 7-day Pro trial", "Compare plans", "on by default"):
        assert sale not in rendered["text"]
    html = INDEX.read_text(encoding="utf-8")
    preferences = html[html.index('id="manage-settings-panel"'):]
    assert "I approve readable managed processing for the selected workspace." in preferences


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_consent_panel_for_an_unconnected_installation_names_both_steps(tmp_path, view):
    rendered = _run_commercial(tmp_path, [{
        "name": "unconnected", "kind": "failure", "view": view,
        "error": {"status": 409, "code": "consent_required"},
    }])["unconnected"]

    assert rendered["failure"]["action"] == "consent"
    assert ("After connecting to Engraphis Cloud, approve each workspace in "
            "Settings → Preferences") in rendered["text"]
    assert "Your plan includes" not in rendered["text"]


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation", "sync"])
@pytest.mark.parametrize("status", [402, 501])
def test_a_genuine_entitlement_failure_still_routes_to_plans(tmp_path, view, status):
    """402 not subscribed and 501 not offered are billing answers."""

    rendered = _run_commercial(tmp_path, [{
        "name": "unentitled", "kind": "failure", "view": view, "error": {"status": status},
    }])["unentitled"]

    assert rendered["failure"]["action"] == "plan"
    assert rendered["failure"]["title"] == "%s needs Engraphis Pro" % FEATURES[view]
    assert "An email-confirmed, no-card Pro trial lasts exactly 7 active days." in rendered["text"]
    assert _labels(rendered) == ["Compare plans"]


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("access,reason,action", [
    ("trial", "Your free trial is live until", "Open Engraphis Cloud"),
    ("trial_expired", "Your free trial has ended", "Subscribe to Pro"),
    ("lapsed", "no longer active", "Update billing"),
    ("active", "does not include this", "Open Engraphis Cloud"),
])
def test_a_refusal_never_offers_a_trial_the_server_would_refuse(
    tmp_path, view, access, reason, action,
):
    """``start_trial`` refuses every organization that already holds an entitlement.

    The refusal explains which of the four situations the customer is in, and the Plans tab it
    routes to offers the one action that can succeed there — never another trial.
    """

    license = {
        "plan": "pro", "access_state": access, "plan_source": "cloud",
        "trial": {"used": access != "active", "active": access == "trial",
                  "available": False, "ends_at": 4102444800},
    }
    rendered = _run_commercial(tmp_path, [
        {"name": "gated", "kind": "failure", "view": view, "error": {"status": 402},
         "license": license},
        {"name": "cta", "kind": "cta", "plan": "pro", "license": license},
    ])

    assert rendered["gated"]["failure"]["action"] == "plan"
    assert reason in rendered["gated"]["text"]
    assert "Start 7-day Pro trial" not in rendered["gated"]["text"]
    assert rendered["cta"]["label"] == action
    assert "trial" not in rendered["cta"]["label"].lower()


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
@pytest.mark.parametrize("status", [400, 401, 500, 503])
def test_a_non_billing_failure_never_sells_pro(tmp_path, view, status):
    """A bad request, an expired session, or a cloud outage is not an unpaid invoice.

    401 is a credential problem: the cloud maps it to "the session expired or was revoked;
    connect again", so a paying customer is told to reconnect rather than sold their plan.
    """

    rendered = _run_commercial(tmp_path, [{
        "name": "broken", "kind": "failure", "view": view,
        "message": "the hosted service is briefly unavailable", "error": {"status": status},
        "license": {"plan": "pro", "access_state": "active", "plan_source": "cloud",
                    "trial": {"available": False}},
    }])["broken"]

    assert "Compare plans" not in _labels(rendered)
    assert "needs Engraphis Pro" not in rendered["text"]
    if status == 401:
        assert rendered["failure"]["action"] == "reconnect"
        assert rendered["failure"]["title"] == "Reconnect Engraphis Cloud"
        assert "your plan is unchanged" in rendered["text"]
        assert rendered["actions"] == [{
            "link": "Open Engraphis Cloud",
            "href": "https://engraphis.example/account?utm_source=engraphis&utm_medium=product"
                    "&utm_campaign=pro_conversion&utm_content=reconnect",
        }]
    else:
        assert rendered["failure"] is None
        assert "the hosted service is briefly unavailable" in rendered["text"]


@requires_node
@pytest.mark.parametrize("view", ["analytics", "automation"])
def test_a_transient_hosted_conflict_is_not_answered_with_a_purchase_panel(tmp_path, view):
    """A 409 that is not ``consent_required`` is a state conflict, not a billing answer."""

    rendered = _run_commercial(tmp_path, [{
        "name": "conflict", "kind": "failure", "view": view,
        "message": "managed snapshot generation must advance",
        "error": {"status": 409, "code": "generation_conflict"},
    }])["conflict"]

    assert rendered["failure"] is None
    assert "Compare plans" not in _labels(rendered)
    assert "managed snapshot generation must advance" in rendered["text"]


@requires_node
def test_only_an_entitlement_answer_may_route_to_plans(tmp_path):
    """Sweep every status the hosted proxy can return through the one classifier."""

    statuses = [400, 401, 402, 403, 404, 409, 413, 422, 429, 500, 501, 502, 503, 504]
    cases = [
        {"name": "%s-%s-%s" % (status, code or "none", trial), "kind": "failure",
         "view": "analytics", "error": {"status": status, "code": code},
         "license": {"trial": {"available": trial, "trial_days": 7}}}
        for status in statuses
        for code in (None, "consent_required", "cloud_unconfigured")
        for trial in (True, False)
    ]
    rendered = _run_commercial(tmp_path, cases)

    for case in cases:
        failure = rendered[case["name"]]["failure"]
        status, code = case["error"]["status"], case["error"]["code"]
        trial = case["license"]["trial"]["available"]
        selling = bool(failure) and failure["action"] == "plan"
        expected = status in (402, 501) or (
            status == 401 and code == "cloud_unconfigured" and trial)
        assert selling is expected, case["name"]
        if status == 409 and code == "consent_required":
            assert failure["action"] == "consent", case["name"]


@requires_node
@pytest.mark.parametrize("plan", ["team", "pro", ""])
def test_a_lapsed_customer_uses_the_plan_neutral_account_portal(tmp_path, plan):
    """Billing recovery is never reframed as a new plan checkout, whatever the plan reads."""

    cta = _run_commercial(tmp_path, [{
        "name": "lapsed", "kind": "cta", "plan": plan or "pro",
        "license": {"plan": plan, "access_state": "lapsed", "plan_source": "cloud",
                    "trial": {"available": False}},
    }])["lapsed"]

    assert cta["label"] == "Update billing"
    assert cta["href"].startswith("https://engraphis.example/account?utm_source=engraphis")
    assert "checkout/" not in cta["href"]
    assert "plan=" not in cta["href"]


@requires_node
@pytest.mark.parametrize("access,expected,absent", [
    ("inactive", "Start 7-day Pro trial", "Subscribe to Pro"),
    ("trial_expired", "Subscribe to Pro", "Start 7-day Pro trial"),
    ("trial", "Open Engraphis Cloud", "Start 7-day Pro trial"),
    ("active", "Open Engraphis Cloud", "Start 7-day Pro trial"),
])
def test_each_access_state_offers_the_one_action_that_can_succeed(
    tmp_path, access, expected, absent,
):
    cta = _run_commercial(tmp_path, [{
        "name": access, "kind": "cta", "plan": "pro",
        "license": {"plan": "local" if access == "inactive" else "pro", "access_state": access,
                    "plan_source": "local" if access == "inactive" else "cloud",
                    "trial": {"used": access != "inactive", "active": access == "trial",
                              "available": access == "inactive", "ends_at": 0,
                              "trial_days": 7, "days_by_plan": {"pro": 7, "team": 14}}},
    }])[access]

    assert cta["label"] == expected
    assert cta["label"] != absent


@requires_node
def test_a_paying_team_customer_is_not_told_team_is_excluded(tmp_path):
    """The Team tab describes the hosted service; it is not an answer to a denial."""

    rows = _run_commercial(tmp_path, [
        {"name": "team-active", "kind": "team",
         "license": {"plan": "team", "access_state": "active", "plan_source": "cloud"}},
        {"name": "team-trial", "kind": "team",
         "license": {"plan": "team", "access_state": "trial", "plan_source": "cloud",
                     "trial": {"used": True, "active": True, "available": False,
                               "ends_at": 4102444800}}},
        {"name": "pro-active", "kind": "team",
         "license": {"plan": "pro", "access_state": "active", "plan_source": "cloud"}},
        {"name": "team-lapsed", "kind": "team",
         "license": {"plan": "team", "access_state": "lapsed", "plan_source": "cloud"}},
    ])

    assert rows["team-active"]["text"].startswith(
        "Your Team subscription is active. Organizations, roles and seats are managed in "
        "Engraphis Cloud.")
    assert rows["team-trial"]["text"].startswith("Your free trial includes Team until ")
    for name in ("team-active", "team-trial"):
        assert "does not include" not in rows[name]["text"], name
        assert "Plan" in rows[name]["text"]
    # A Pro subscriber is told what Team is, and a lapsed plan that billing is the blocker.
    assert rows["pro-active"]["text"].startswith("Team is a hosted plan.")
    assert "paused until billing is up to date" in rows["team-lapsed"]["text"]


@requires_node
@pytest.mark.parametrize("access,expected", [
    ("trial_expired", ["Your free trial has ended", "local database", "trial runs once"]),
    ("lapsed", ["subscription is no longer active", "The last payment did not go through",
                "Local memories are unaffected"]),
    ("inactive", ["No hosted plan on this installation", "free and complete"]),
])
def test_the_dashboard_explains_a_locked_state_and_what_is_still_safe(tmp_path, access, expected):
    rendered = _run_commercial(tmp_path, [{
        "name": access, "kind": "status",
        "license": {"plan": "local" if access == "inactive" else "pro", "access_state": access,
                    "entitlement_status": "past_due" if access == "lapsed" else "",
                    "plan_source": "local" if access == "inactive" else "cloud"},
    }])[access]

    for phrase in expected:
        assert phrase in rendered["text"], phrase


def test_sync_status_does_not_sell_pro_to_a_customer_who_already_owns_it():
    """Sync status reports the real cause; a refused Sync now goes through the classifier.

    Only the classifier may draw a next step (its routing is swept above), so a dropped
    connection or a relay 5xx can never tell a paying customer to buy the plan they own.
    """

    status = _ledger_declaration("loadSync")
    failure = status[status.index("catch (error)"):]
    assert "error.message" in failure
    for sale in ("hostedCta(", "hostedFailure(", "renderHostedFailure(", "Compare plans"):
        assert sale not in failure, sale

    run = _ledger_declaration("runCloudSync")
    failure = run[run.index("catch (error)"):]
    assert "Cloud Sync failed: ${error.message}" in failure
    assert "const failure = hostedFailure('sync', error);" in failure
    assert failure.index("hostedFailure('sync', error)") < failure.index("renderHostedFailure(")
    assert "hostedCta(" not in failure


def test_plans_list_every_hosted_benefit_and_use_the_state_specific_cta():
    plans = _ledger_declaration("renderPlans")
    benefits = _ledger_declaration("PLAN_BENEFITS")
    styles = STYLES.read_text(encoding="utf-8")

    assert "hostedCta(plan.id, 'plans', interval)" in plans
    assert "Support continued Engraphis development with Pro." in plans
    for benefit in (
        "Hosted Cloud Sync across your installations",
        "Growth, retention, decay and entity Analytics",
        "Auto Consolidation with hosted retention policies",
        "Auto Dreaming with reviewable managed proposals",
        "Private account and billing support",
        "Everything in Pro",
        "Hosted organizations, invitations and named seats",
        "Roles, scoped credentials and Team audit history",
    ):
        assert benefit in benefits
    prices = _ledger_declaration("planPrices")
    for price in ("$10 / owner / month", "$100 / owner / year", "$20 / seat / month",
                  "$200 / seat / year"):
        assert price in prices
    hosted = " ".join((ROOT / "docs" / "HOSTED_PLANS.md").read_text(encoding="utf-8").split())
    assert "Pro: $10/month or $100/year" in hosted
    assert "Team: $20/seat/month or $200/seat/year" in hosted
    assert ".plan-benefit-list" in styles


def test_team_invitations_and_password_setup_are_not_in_local_client():
    html = INDEX.read_text(encoding="utf-8")
    script = SCRIPT.read_text(encoding="utf-8")
    for removed in (
        "getInvitationToken", "showInvitationForm", "Accept team invitation",
        "Confirm password", "/auth/invitations/accept", "invite_token", "reset_token",
    ):
        assert removed not in script
        assert removed not in html
    assert 'id="auth-overlay"' not in html
    assert ("Per-member agent accounts, roles, named seats and rotating device credentials "
            "are managed in Engraphis Cloud") in _ledger_declaration("renderTeamState")


def test_untrusted_values_never_reach_markup_or_inline_handlers():
    """Ledger builds every value with textContent; nothing is parsed as HTML or as a handler."""

    html = INDEX.read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler in index.html"
    for path in sorted(ASSETS.glob("*.js")):
        if path.name.startswith("engraphis-graph"):
            continue  # the engine's tooltip sink is covered by test_graph_engine_asset.py
        source = path.read_text(encoding="utf-8")
        for sink in (".innerHTML", "insertAdjacentHTML", ".outerHTML", "document.write("):
            assert sink not in source, (path.name, sink)
