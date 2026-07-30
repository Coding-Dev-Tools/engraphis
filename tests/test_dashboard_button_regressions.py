"""Static regression checks for dashboard button failure modes found in manual QA."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LEDGER_HTML = ROOT / "engraphis" / "dashboard_assets" / "index.html"
LEDGER_JS = ROOT / "engraphis" / "dashboard_assets" / "ledger.js"
LEDGER_CSS = ROOT / "engraphis" / "dashboard_assets" / "ledger.css"
CLASSIC_JS = ROOT / "engraphis" / "classic_assets" / "dashboard.js"


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
        "supersession-form",
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


def test_classic_escape_closes_mobile_nav_and_returns_focus():
    script = CLASSIC_JS.read_text(encoding="utf-8")

    assert "if(event.key!=='Escape')return;" in script
    assert "closeMobileNav(true);return" in script


def test_validation_error_has_visible_error_styling():
    css = LEDGER_CSS.read_text(encoding="utf-8")

    assert ".form-error" in css
    assert "color: var(--c-bad);" in css
