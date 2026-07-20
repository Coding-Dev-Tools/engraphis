"""Regressions for auth/security fixes: atomic setup, cookie Secure behind a proxy, and
the redirector open-redirect."""
from concurrent.futures import ThreadPoolExecutor
import threading

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


def test_inspector_setup_atomically_creates_exactly_one_admin(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from engraphis import licensing
    from engraphis.config import settings
    from engraphis.inspector.app import create_app
    from engraphis.inspector.auth import AuthStore

    monkeypatch.setattr(settings, "team_mode", True)
    monkeypatch.setattr(settings, "api_token", "")
    monkeypatch.setattr(licensing, "has_feature", lambda feature: feature == "team")
    store = AuthStore(str(tmp_path / "users.db"), iterations=1000)

    # Force both HTTP requests past the router's optimistic zero-user check before
    # either enters create_user. The store's require_empty transaction must decide
    # the winner; without the route keyword both different-email admins are created.
    original_count = store.count_users
    precheck = threading.Barrier(2)

    def racing_count():
        count = original_count()
        if count == 0:
            precheck.wait(timeout=5)
        return count

    monkeypatch.setattr(store, "count_users", racing_count)
    payloads = [
        {"email": "first@example.com", "name": "First", "password": "supersecret1"},
        {"email": "second@example.com", "name": "Second", "password": "supersecret2"},
    ]
    app = create_app(auth_store=store)
    clients = [
        TestClient(app, client=("127.0.0.1", 50000)),
        TestClient(app, client=("127.0.0.1", 50001)),
    ]
    with clients[0], clients[1]:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(
                lambda item: item[0].post("/api/auth/setup", json=item[1]),
                zip(clients, payloads),
            ))

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert original_count() == 1


def test_cookie_secure_honors_only_trusted_forwarded_proto(monkeypatch):
    from engraphis.routes.v2_team import _cookie_secure

    class _Req:
        def __init__(self, scheme, headers):
            self.url = type("U", (), {"scheme": scheme})()
            self.headers = headers

    assert _cookie_secure(_Req("https", {})) is True
    assert _cookie_secure(_Req("http", {"x-forwarded-proto": "https"})) is False
    monkeypatch.setenv("ENGRAPHIS_FORWARDED_ALLOW_IPS", "*")
    # The trusted proxy's appended (rightmost) scheme wins over a preseeded value.
    assert _cookie_secure(_Req("http", {"x-forwarded-proto": "http, https"})) is True
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
    assert r.headers["x-frame-options"] == "DENY"
