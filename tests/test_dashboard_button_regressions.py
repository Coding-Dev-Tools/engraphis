"""Static regression checks for dashboard button failure modes found in manual QA."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER_HTML = ROOT / "engraphis" / "dashboard_assets" / "index.html"
LEDGER_JS = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
LEDGER_CSS = ROOT / "engraphis" / "dashboard_assets" / "ledger.css"


def test_memory_editor_reports_empty_content_and_restores_trigger_focus():
    html = LEDGER_HTML.read_text(encoding="utf-8")
    script = LEDGER_JS.read_text(encoding="utf-8")

    assert '<form id="memory-editor" class="detail-panel editor-panel" novalidate hidden>' in html
    assert 'id="editor-error" class="form-error" role="alert" hidden' in html
    assert "editorReturnFocus" in script
    assert "Enter memory content before saving." in script
    assert "contentField.setAttribute('aria-invalid', 'true')" in script
    assert "returnFocus.focus()" in script


def test_empty_dashboard_forms_reach_accessible_validation_handlers():
    html = LEDGER_HTML.read_text(encoding="utf-8")
    script = LEDGER_JS.read_text(encoding="utf-8")

    for form_id in (
        "ask-form",
        "why-form",
        "timeline-form",
        "create-workspace-form",
    ):
        assert f'<form id="{form_id}"' in html
        form = html[html.index(f'<form id="{form_id}"'):]
        assert "novalidate" in form[:180]
    for message in (
        "Enter a question before requesting a grounded answer.",
        "Enter a claim or topic before tracing belief.",
        "Enter a topic before",
        "Enter a workspace name before creating it.",
    ):
        assert message in script


def test_successful_dashboard_actions_clear_stale_validation_notices():
    script = LEDGER_JS.read_text(encoding="utf-8")

    for function_name in ("askMemory", "whySearch", "timelineSearch", "createWorkspace"):
        start = script.index(f"async function {function_name}")
        end = script.find("\n  async function ", start + 1)
        section = script[start:] if end == -1 else script[start:end]
        assert "showNotice('');" in section


def test_rechecks_never_reselect_the_workspace_under_open_work():
    """Embedder recheck and connection retry must keep an open graph and unsaved edits.

    Re-running the whole bootstrap reselects the workspace, which tears down the graph
    renderer and resets the memory editor.
    """
    script = LEDGER_JS.read_text(encoding="utf-8")
    recheck = script[script.index("  async function recheckEmbedder()"):]
    recheck = recheck[:recheck.index("\n  }\n")]
    assert "refreshBootstrap(" not in recheck
    assert "renderEmbedderBanner(bootstrap.embedder);" in recheck
    retry = script[script.index("  async function retryConnection()"):]
    retry = retry[:retry.index("\n  }\n")]
    booted = retry[retry.index("if (!state.booted) await boot();"):]
    assert "refreshBootstrap(" not in booted
    assert "await api('/health'" in booted


def test_validation_error_has_visible_error_styling():
    css = LEDGER_CSS.read_text(encoding="utf-8")

    assert ".form-error" in css
    assert "color: var(--c-bad);" in css
