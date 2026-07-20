"""Static release-infrastructure invariants that must not drift silently."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_published_image_and_railway_template_fail_safe_to_customer_mode():
    dockerfile = _text("Dockerfile")
    template = json.loads(_text("deploy/railway-template.json"))
    railway = json.loads(_text("railway.json"))

    assert "ENGRAPHIS_SERVICE_MODE=customer" in dockerfile
    assert railway["$schema"] == "https://railway.com/railway.schema.json"
    assert template["format"] == "engraphis-railway-template-composer-source/v1"
    assert template["variables"]["ENGRAPHIS_SERVICE_MODE"]["value"] == "customer"
    assert template["service"]["healthcheck"] == "/api/ready"
    assert template["service"]["volume"]["mount_path"] == "/data"
    deployment = template["variables"]["ENGRAPHIS_DEPLOYMENT_TOKEN"]
    assert deployment["value"] == "${{ secret(48) }}"
    assert deployment["secret"] is True
    assert deployment["required"] is True


def test_recurring_customer_operations_never_use_deployment_token():
    for workflow in (
            ".github/workflows/commercial-backup.yml",
            ".github/workflows/production-synthetics.yml"):
        content = _text(workflow)
        assert "secrets.ENGRAPHIS_CUSTOMER_OPS_TOKEN" in content
        assert "secrets.ENGRAPHIS_CUSTOMER_DEPLOYMENT_TOKEN" not in content


def test_backup_workflow_verifies_authenticated_backup_readiness():
    content = _text(".github/workflows/commercial-backup.yml")
    readiness = content.split("- name: Require fresh readiness after backup", 1)[1]

    assert '"$CUSTOMER_TOKEN"' in readiness
    assert '"${CUSTOMER_URL%/}/api/ops/ready"' in readiness
    assert '"${CUSTOMER_URL%/}/api/ready"' not in readiness
    assert '"$VENDOR_TOKEN"' in readiness
    assert '"${LICENSE_URL%/}/ops/ready"' in readiness


def test_ci_and_release_audit_production_image_dependencies():
    ci = _text(".github/workflows/ci.yml")
    release = _text(".github/workflows/release.yml")

    assert "Audit the exact production image dependency set" in ci
    assert "docker run --rm --entrypoint sh engraphis:ci" in ci
    assert "python -m pip_audit --local" in ci
    assert "tesseract-ocr" in _text("Dockerfile")
    assert "Verify production image OCR runtime" in ci
    assert "Verify production image OCR runtime" in release
    assert "docker-entrypoint\\.sh" in ci
    assert "railway\\.json" in ci
    assert "deploy/" in ci
    assert '".[all,test]"' in release
    assert "Audit production image dependencies" in release
    assert "Browser accessibility release gate" in release
    assert "Require release tag commit to be on protected main" in release
    for version in ('"3.9"', '"3.10"', '"3.11"', '"3.12"'):
        assert version in release


def test_all_workflow_actions_are_pinned_to_full_commit_shas():
    workflows = ROOT / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        for line_number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if not stripped.startswith("uses:") and "- uses:" not in stripped:
                continue
            reference = stripped.split("uses:", 1)[1].strip().split()[0]
            assert "@" in reference, f"{path.name}:{line_number} has no action ref"
            revision = reference.rsplit("@", 1)[1]
            assert len(revision) == 40 and all(c in "0123456789abcdef" for c in revision), (
                f"{path.name}:{line_number} action is not pinned to a full commit SHA"
            )


def test_ci_and_release_default_to_read_only_repository_permissions():
    for workflow in (
            ".github/workflows/ci.yml",
            ".github/workflows/release.yml"):
        header = _text(workflow).split("\njobs:", 1)[0]
        assert "\npermissions:\n  contents: read\n" in header
