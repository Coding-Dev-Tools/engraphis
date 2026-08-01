"""Railway deployment contracts that can be checked without a live deployment."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from scripts import start_dashboard


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_railway_manifest_builds_the_runtime_image_and_uses_readiness():
    manifest = json.loads(_text("railway.json"))

    assert manifest["$schema"] == "https://railway.com/railway.schema.json"
    assert manifest["build"] == {"builder": "DOCKERFILE", "dockerfilePath": "Dockerfile"}
    assert manifest["deploy"] == {
        "healthcheckPath": "/api/ready",
        "healthcheckTimeout": 300,
        "restartPolicyType": "ON_FAILURE",
        "restartPolicyMaxRetries": 10,
    }


def test_container_runtime_matches_the_railway_persistence_and_port_contract():
    dockerfile = _text("Dockerfile")
    entrypoint = _text("docker-entrypoint.sh")

    assert "EXPOSE 8700" in dockerfile
    assert 'ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]' in dockerfile
    assert 'CMD ["engraphis-dashboard", "--no-open"]' in dockerfile
    assert "os.environ.get('PORT') or os.environ.get('ENGRAPHIS_PORT','8700')" in dockerfile
    assert "useradd --create-home --uid 10001 engraphis" in dockerfile
    assert "HF_HOME=/data/.cache/huggingface" in dockerfile
    assert "ENGRAPHIS_STATE_DIR=/data/.engraphis" in dockerfile

    assert 'if [ -z "${ENGRAPHIS_HOST:-}" ]; then' in entrypoint
    assert '[ -n "${RAILWAY_SERVICE_NAME:-}" ]' in entrypoint
    assert "ENGRAPHIS_HOST=\"::\"" in entrypoint
    assert "ENGRAPHIS_HOST=\"0.0.0.0\"" in entrypoint
    assert "chown -R engraphis:engraphis /data" in entrypoint
    assert 'exec gosu engraphis "$@"' in entrypoint


def test_railway_image_is_cpu_only_and_installs_only_its_runtime_surface():
    """A Railway web image must not silently download CUDA or unrelated optional tools."""
    dockerfile = _text("Dockerfile")

    assert "https://download.pytorch.org/whl/cpu torch" in dockerfile
    assert 'pip install ".[server,documents,cloud-sync]"' in dockerfile
    assert 'pip install ".[all]"' not in dockerfile


def test_platform_port_precedes_a_fixed_engraphis_port(monkeypatch):
    """Railway routes and probes the port injected as ``PORT``, not 8700."""
    uvicorn = pytest.importorskip("uvicorn")
    captured = {}
    monkeypatch.setenv("PORT", "8791")
    monkeypatch.setenv("ENGRAPHIS_PORT", "8700")
    monkeypatch.setattr(start_dashboard, "_port_is_available", lambda *_args: True)
    monkeypatch.setattr(
        uvicorn, "run", lambda _app, **kwargs: captured.update(kwargs),
    )
    fake_dashboard = types.ModuleType("engraphis.dashboard_app")
    fake_dashboard.app = object()
    monkeypatch.setitem(sys.modules, "engraphis.dashboard_app", fake_dashboard)

    start_dashboard.main(["--no-open"])

    assert captured["port"] == 8791
    assert captured["host"] == start_dashboard.os.environ.get("ENGRAPHIS_HOST", "127.0.0.1")
    assert start_dashboard.os.environ["ENGRAPHIS_PORT"] == "8791"
