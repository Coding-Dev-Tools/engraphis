"""Integrity and provenance contracts for browser bundles committed to the repository."""
import hashlib
import json
import re
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


def test_csp_safe_d3_bundle_ships_exactly_once():
    """The patched d3 build has one home, so no second copy can drift from it.

    d3-dsv builds its CSV row converter with the Function constructor, which the production
    CSP (``script-src 'self'``, no ``'unsafe-eval'``) blocks; the vendored build replaces it
    with an equivalent closure.
    """
    copies = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "engraphis").rglob("d3.min.js")
    )
    assert copies == ["engraphis/dashboard_assets/vendor/d3.min.js"]
    assert "new Function" not in (VENDOR / "d3.min.js").read_text(encoding="utf-8")


def test_design_linter_dependency_is_exactly_pinned():
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    version = package["devDependencies"]["impeccable"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    assert package_lock["packages"]["node_modules/impeccable"]["version"] == version
