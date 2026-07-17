"""Regressions for auth/security fixes: atomic setup, cookie Secure behind a proxy, and
the redirector open-redirect."""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")


def test_create_user_require_empty_is_atomic_bootstrap(tmp_path):
    from engraphis.inspector.auth import AuthError, AuthStore
    store = AuthStore(str(tmp_path / "users.db"), iterations=1000)
    store.create_user("admin@x.co", "Admin", "Sup3rSecret!", "admin", require_empty=True)
    # A second require_empty bootstrap must be refused — /api/auth/setup creates only the
    # first admin, atomically (closes the multi-admin TOCTOU).
    with pytest.raises(AuthError):
        store.create_user("eve@x.co", "Eve", "Sup3rSecret!", "admin", require_empty=True)
    assert store.count_users() == 1


def test_cookie_secure_honors_forwarded_proto():
    from engraphis.routes.v2_team import _cookie_secure

    class _Req:
        def __init__(self, scheme, headers):
            self.url = type("U", (), {"scheme": scheme})()
            self.headers = headers

    assert _cookie_secure(_Req("https", {})) is True
    # Behind a TLS-terminating proxy the internal scheme is http but XFP says https.
    assert _cookie_secure(_Req("http", {"x-forwarded-proto": "https"})) is True
    assert _cookie_secure(_Req("http", {})) is False


def test_redirector_ignores_spoofed_host(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("ENGRAPHIS_DASHBOARD_URL", raising=False)
    monkeypatch.setenv("ENGRAPHIS_HOST", "127.0.0.1")
    from engraphis.redirector import create_app
    c = TestClient(create_app())
    r = c.get("/memories?q=1", headers={"X-Forwarded-Host": "evil.com",
                                        "Host": "evil.com"}, follow_redirects=False)
    assert r.status_code == 301
    loc = r.headers["location"]
    assert "evil.com" not in loc                     # spoofed host is not reflected
    assert loc.startswith("http://127.0.0.1:8700/memories")


def test_redirector_uses_configured_dashboard_url(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("ENGRAPHIS_DASHBOARD_URL", "https://dash.example.com")
    from engraphis.redirector import create_app
    c = TestClient(create_app())
    r = c.get("/x", headers={"X-Forwarded-Host": "evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "https://dash.example.com/x"
