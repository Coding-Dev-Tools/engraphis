"""Static UI contract for the single-user local client and hosted commercial boundary."""
import re
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"
SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.js"


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
        assert "e.status===401||e.status===402||e.status===501" in body
        assert "unlockHtml" in body


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
