"""Regression: the cloud license + sync-relay endpoints must be mounted on the
*shipped* entrypoints — not only on the retired Inspector.

The bug this guards: ``/license/v1/*`` (register / verify / **revoke**) and ``/relay/v1``
lived solely on ``engraphis.inspector.app``. After the Inspector was retired, the
deployed binaries (``engraphis.app`` = public server, ``engraphis.dashboard_app`` = team
dashboard) served neither — so key **revocation was inoperable in production** and Pro
sync had no backend. An unmounted route 404s; a mounted-but-license-gated route answers
402/401. We assert the latter on both apps. (Same style as
tests/test_billing.py::test_main_public_server_mounts_the_route.)
"""
import pytest

pytest.importorskip("fastapi", reason="full-stack extra not installed")
from fastapi.testclient import TestClient  # noqa: E402


def _main_app(monkeypatch):
    monkeypatch.setenv("ENGRAPHIS_DB_PATH", ":memory:")
    monkeypatch.setenv("ENGRAPHIS_LOOP_INTERVAL", "0")
    from engraphis.app import create_app
    return TestClient(create_app())


def _dashboard_app(monkeypatch, tmp_path):
    monkeypatch.setattr("engraphis.config.settings.db_path", str(tmp_path / "d.db"))
    from engraphis.dashboard_app import create_app
    return TestClient(create_app())


@pytest.mark.parametrize("which", ["main", "dashboard"])
def test_cloud_license_endpoints_are_mounted(which, monkeypatch, tmp_path):
    c = _main_app(monkeypatch) if which == "main" else _dashboard_app(monkeypatch, tmp_path)

    # register with a bad key: mounted+gated => 402 (LicenseError handler wired),
    # NOT 404 (unmounted) and NOT 500 (missing handler).
    r = c.post("/license/v1/register", json={"key": "not-a-key", "machine_id": "m1"})
    assert r.status_code == 402, (
        f"regression: /license/v1/register not mounted/handled on {which} "
        f"(got {r.status_code}: {r.text[:200]})")

    # verify is a public status probe — must answer JSON, not 404.
    rv = c.get("/license/v1/verify/deadbeef")
    assert rv.status_code == 200 and rv.json()["known"] is False

    # revoke requires the vendor admin token: mounted => 401 without it (not 404).
    rr = c.post("/license/v1/revoke/deadbeef")
    assert rr.status_code == 401, (
        f"regression: /license/v1/revoke not mounted on {which} (got {rr.status_code})")


@pytest.mark.parametrize("which", ["main", "dashboard"])
def test_sync_relay_is_mounted_and_license_gated(which, monkeypatch, tmp_path):
    c = _main_app(monkeypatch) if which == "main" else _dashboard_app(monkeypatch, tmp_path)
    # No Bearer license => the relay must reject with 402 (gated), not 404 (unmounted).
    r = c.get("/relay/v1/demo/names")
    assert r.status_code == 402, (
        f"regression: /relay/v1 not mounted/gated on {which} (got {r.status_code}: {r.text[:200]})")
