"""Static UI contract for clear account sign-in versus license activation."""
import re
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"
SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.js"


def test_dashboard_sign_in_is_in_topbar_and_license_key_activates():
    html = INDEX.read_text(encoding="utf-8")

    # Assert containment, not merely source order: a later control would satisfy
    # ``topbar < session_action`` while still rendering outside the topbar.
    topbar = html.index('<header class="topbar">')
    topbar_end = html.index("</header>", topbar)
    session_action = html.index('id="session-action"')
    assert topbar < session_action < topbar_end
    script = SCRIPT.read_text(encoding="utf-8")
    assert "async function activateLicense()" in script
    assert "async function activateSyncLicense()" in script
    assert "function signInSync()" not in script


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


def test_hosted_first_boot_has_an_actionable_non_data_setup_screen():
    script = SCRIPT.read_text(encoding="utf-8")
    hosted = script[script.index("function renderHostedBootstrap"):
                    script.index("async function showHostedBootstrap")]

    assert "Hosted onboarding" in hosted
    assert "ENGRAPHIS_DEPLOYMENT_TOKEN" in hosted
    assert "startTrialPlan" in script
    assert "activateLicense()" not in hosted
    assert "else if(e.status===403){await showHostedBootstrap(e.message);resumeTrialClaim()}" \
        in script


def test_invitation_link_opens_recipient_password_setup():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "getInvitationToken" in script
    assert "showInvitationForm" in script
    assert "Accept team invitation" in script
    assert "Confirm password" in script
    assert "'/auth/invitations/accept'" in script
    assert "url.searchParams.delete('invite_token')" in script
    assert "new URLSearchParams(raw).get(name)" in script
    assert "new URLSearchParams(location.search).get(name)" not in script
    assert "params.delete('invite_token')" in script
    assert "params.delete('reset_token')" in script
    startup = script[script.index("(async function(){resumeTrialClaim()"):
                     script.index("setInterval(checkHealth")]
    assert startup.index("scrubAuthLinkTokens()") < startup.index("if(INVITE_TOKEN)")
    assert "if(AUTH_MODE==='invitation'){cancelInvitation();return}" in script
    assert "document.getElementById('topbar-title')" in script


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
