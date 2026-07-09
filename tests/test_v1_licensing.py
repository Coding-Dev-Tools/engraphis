"""v1 REST licensing: /memory/license, activation, and the analytics/export paywall.

Skips on the numpy-only CI gate (needs fastapi/httpx), like test_app_auth.py.
"""
import tempfile
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
pytest.importorskip("httpx", reason="httpx not installed")

from fastapi.testclient import TestClient  # noqa: E402

from engraphis import licensing as lic  # noqa: E402
from engraphis.config import settings  # noqa: E402
from engraphis.licensing import compose_key, ed25519_public_key  # noqa: E402

_SECRET = bytes(range(32))  # deterministic test vendor key
# One DB for the whole module: the v1 store connection is thread-local and bound to the
# path at open time, so a per-test tmp DB would leave a stale conn in the threadpool.
_DB_PATH = str(Path(tempfile.mkdtemp()) / "lic.db")


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", _DB_PATH)
    # v1 store keeps a process-global thread-local conn; reset it so every thread
    # (incl. reused threadpool workers from earlier test files) reconnects to _DB_PATH.
    monkeypatch.setattr("engraphis.stores._local", threading.local())
    monkeypatch.setattr(settings, "loop_interval", 0)
    monkeypatch.setattr(settings, "embed_model", "")
    monkeypatch.setenv("ENGRAPHIS_LICENSE_PUBKEY", ed25519_public_key(_SECRET).hex())
    monkeypatch.setattr(lic, "_LICENSE_FILE", tmp_path / "license.key")
    from engraphis.app import create_app
    return TestClient(create_app())


def _pro_key():
    return compose_key({"v": 1, "plan": "pro", "email": "b@x.co", "seats": 1,
                        "issued": int(time.time()),
                        "expires": int(time.time() + 365 * 86400)}, _SECRET)


def test_free_tier_gates_analytics_and_export(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
        lic.current_license(refresh=True)
        assert c.get("/memory/license").json()["data"]["plan"] == "free"
        r = c.get("/memory/analytics")
        assert r.status_code == 402
        assert set(r.json()["detail"]) >= {"error", "feature", "tier_required", "upgrade_url"}
        assert c.get("/memory/export").status_code == 402
    lic.current_license(refresh=True)


def test_pro_key_unlocks_analytics_and_export(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        monkeypatch.setenv("ENGRAPHIS_LICENSE_KEY", _pro_key())
        lic.current_license(refresh=True)
        assert c.get("/memory/analytics").status_code == 200
        assert c.get("/memory/export").status_code == 200
    lic.current_license(refresh=True)


def test_activate_endpoint_persists_valid_key_and_rejects_bad(monkeypatch, tmp_path):
    with _client(monkeypatch, tmp_path) as c:
        monkeypatch.delenv("ENGRAPHIS_LICENSE_KEY", raising=False)
        lic.current_license(refresh=True)
        ok = c.post("/memory/license/activate", json={"key": _pro_key()})
        assert ok.status_code == 200 and ok.json()["data"]["plan"] == "pro"
        bad = c.post("/memory/license/activate", json={"key": "ENGR1.bad.key"})
        assert bad.status_code == 400
    lic.current_license(refresh=True)
