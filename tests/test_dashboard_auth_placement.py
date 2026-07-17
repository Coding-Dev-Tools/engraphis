"""Static UI contract for clear account sign-in versus license activation."""
from pathlib import Path


INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"


def test_dashboard_sign_in_is_in_topbar_and_license_key_activates():
    html = INDEX.read_text(encoding="utf-8")

    topbar = html.index('<div class="topbar">')
    session_action = html.index('id="session-action"')
    assert topbar < session_action
    assert 'onclick="activateSyncLicense()">Activate</button>' in html
    assert 'function signInSync()' not in html
