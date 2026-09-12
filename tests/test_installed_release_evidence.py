"""Public installed evidence is complete, artifact-bound and free of machine-local paths."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from scripts.release_evidence import (
    EvidenceError, _INSTALLED_CHECKS, installed_bundle_records, installed_journey_artifacts,
)


@pytest.fixture
def installed_matrix(tmp_path):
    wheel = tmp_path / "engraphis-1.2.3-py3-none-any.whl"
    sources = {"__init__.py": b'__version__ = "1.2.3"\n', "static/main.js": b"// fixture\n"}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, data in sources.items():
            archive.writestr("engraphis/" + name, data)
    source_digest = hashlib.sha256(b"".join(
        name.encode() + b"\0" + data for name, data in sorted(sources.items())
    )).hexdigest()
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    directory, output = tmp_path / "captured", tmp_path / "public"
    directory.mkdir()
    output.mkdir()
    for os_name, platform in {"ubuntu-latest": "linux", "windows-latest": "win32", "macos-latest": "darwin"}.items():
        for profile, checks in _INSTALLED_CHECKS.items():
            cell = directory / ("installed-journey-" + os_name + "-" + profile)
            cell.mkdir()
            report = {
                "format": "engraphis-installed-journey/v1", "version": "1.2.3",
                "package_source_sha256": source_digest, "platform": platform, "python": "3.11.15",
                "installed_artifact": True, "embedding": "deterministic/offline", "checks": {profile: checks},
            }
            (cell / "installed-journey.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
            (cell / "installed-artifact.json").write_text(json.dumps({
                "profile": profile, "wheel": wheel.name, "wheel_sha256": wheel_digest,
            }), encoding="utf-8")
            (cell / "installed-environment.lock").write_text(
                "engraphis @ file:///home/private-person/repo/" + wheel.name + "\n"
                "numpy==2.0.0\npip==26.2\n" + ("mcp==1.30.0\n" if profile == "mcp" else
                                                 "fastapi==0.141.1\nuvicorn==0.52.4\n"), encoding="utf-8",
            )
    return tmp_path, directory, output, wheel


def package(item):
    root, directory, output, wheel = item
    return installed_journey_artifacts(root, directory, output, wheel, "1.2.3")


def first_report(item, filename="installed-journey.json"):
    return next(item[1].iterdir()) / filename


def test_all_six_cells_publish_exact_hashes_and_safe_full_dependency_versions(installed_matrix):
    root, _, output, wheel = installed_matrix
    report = package(installed_matrix)
    assert len(report["cells"]) == 6
    records = installed_bundle_records(report, {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()})
    assert len(records) == len(list(output.iterdir())) == 18
    for record in records:
        raw = (root / record["path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["sha256"]
        assert b"private-person" not in raw and b"file:///" not in raw
        if record["kind"] == "environment":
            assert b"engraphis==1.2.3\n" in raw and b"pip==26.2\n" in raw
    assert package(installed_matrix) == report


@pytest.mark.parametrize("field,value", [
    ("version", "1.2.2"), ("package_source_sha256", "a" * 64), ("platform", "invented"),
    ("python", "3.9.0"), ("installed_artifact", False), ("installed_artifact", "true"),
    ("embedding", "live provider"), ("checks", {"mcp": ["initialize"]}),
    ("operator_notes", "Private customer data"),
])
def test_partial_or_incompatible_journey_cannot_be_published(installed_matrix, field, value):
    path = first_report(installed_matrix)
    report = json.loads(path.read_text(encoding="utf-8"))
    report[field] = value
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(EvidenceError):
        package(installed_matrix)
    assert not list(installed_matrix[2].iterdir())


def test_incomplete_matrix_and_extra_raw_logs_are_rejected(installed_matrix):
    cell = next(installed_matrix[1].iterdir())
    (cell / "raw-private-log.txt").write_text("not public evidence", encoding="utf-8")
    with pytest.raises(EvidenceError, match="exactly its three"):
        package(installed_matrix)
    (cell / "raw-private-log.txt").unlink()
    cell.rename(cell.with_name("unexpected-cell"))
    with pytest.raises(EvidenceError, match="complete six-cell"):
        package(installed_matrix)


@pytest.mark.parametrize("raw", ["engraphis==1.2.3\n", "private @ https://example.invalid/private\n",
                                 "engraphis @ file:///private/other.whl\n", "numpy==not-a-version\n"])
def test_dependency_capture_must_be_complete_public_pins(installed_matrix, raw):
    first_report(installed_matrix, "installed-environment.lock").write_text(raw, encoding="utf-8")
    with pytest.raises(EvidenceError):
        package(installed_matrix)


def test_wheel_identity_and_ambiguous_json_fail_closed(installed_matrix):
    path = first_report(installed_matrix, "installed-artifact.json")
    report = json.loads(path.read_text(encoding="utf-8"))
    report["wheel_sha256"] = "a" * 64
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(EvidenceError, match="different distribution bytes"):
        package(installed_matrix)
    path.write_text('{"profile":"mcp","profile":"server"}', encoding="utf-8")
    with pytest.raises(EvidenceError, match="unambiguous"):
        package(installed_matrix)


@pytest.mark.parametrize("change", ["cell", "file", "path", "identity"])
def test_repair_checks_complete_installed_index(installed_matrix, change):
    report = deepcopy(package(installed_matrix))
    wheel = installed_matrix[3]
    if change == "cell":
        report["cells"].pop()
    elif change == "file":
        report["cells"][0]["files"].pop()
    elif change == "path":
        report["cells"][0]["files"][0]["path"] = "../outside.json"
    else:
        report["wheel_sha256"] = "a" * 64
    with pytest.raises(EvidenceError):
        installed_bundle_records(report, {wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest()})


def test_new_release_requires_downloaded_matrix_and_repairs_verify_its_files():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text(encoding="utf-8")
    evidence = workflow.split("  release-evidence:\n", 1)[1].split("  publish:\n", 1)[0]
    repair = workflow.split("  github-release-repair:\n", 1)[1]
    assert "pattern: installed-journey-*" in evidence
    assert "--installed-journeys installed-journey-inputs" in evidence
    assert "records.extend(installed_bundle_records" in repair
    assert 'if "installed_journeys" in evidence:' in repair  # Older format-3 bundles remain repairable.
