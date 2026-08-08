"""Integrity and provenance contracts for browser bundles committed to the repository."""
import hashlib
import json
from pathlib import Path


VENDOR = Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets" / "vendor"
ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_vendor_manifest_pins_every_bundle_and_license():
    manifest = json.loads((VENDOR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "engraphis-vendored-assets/v1"
    assets = {entry["path"]: entry for entry in manifest["assets"]}
    assert set(assets) == {"d3.min.js", "force-graph.min.js"}

    for filename, entry in assets.items():
        payload = (VENDOR / filename).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        assert entry["version"] and entry["version"] in entry["source"]
        assert "latest" not in entry["source"]
        assert (VENDOR / entry["license_file"]).is_file()

    assert assets["force-graph.min.js"]["local_modifications"]
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    force_graph_version = assets["force-graph.min.js"]["version"]
    assert package["devDependencies"]["force-graph"] == force_graph_version
    assert package_lock["packages"]["node_modules/force-graph"]["version"] == force_graph_version


def test_design_linter_dependency_is_exactly_pinned():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    version = package["devDependencies"]["impeccable"]
    assert version == "3.5.0"
    assert package_lock["packages"]["node_modules/impeccable"]["version"] == version
