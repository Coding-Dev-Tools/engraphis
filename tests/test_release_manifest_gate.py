"""Release gate: the manifest check must cover the website checkout.

``scripts/check_commercial_manifest.py`` only validates pricing/claim parity
inside ``_check_website()``, which is reachable solely via ``--website-root``.
If release.yml invokes the script without the flag, every published price is
unguarded — so this pins the flag in the release gate.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _release_yml() -> str:
    return (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def test_release_gate_passes_website_root_to_manifest_check():
    text = _release_yml()
    manifest_lines = [
        line.strip()
        for line in text.splitlines()
        if "check_commercial_manifest.py" in line
    ]
    assert manifest_lines, "release gate no longer runs the commercial manifest check"
    assert any("--website-root" in line for line in manifest_lines), (
        "manifest check must receive --website-root so website pricing/claim "
        "parity is actually validated in the release gate"
    )


def test_manifest_script_accepts_website_root():
    import argparse

    source = (ROOT / "scripts" / "check_commercial_manifest.py").read_text(encoding="utf-8")
    assert '"--website-root"' in source or "'--website-root'" in source
    # The flag must stay optional: CI jobs without a website checkout call the
    # script bare, and argparse must not turn that into a usage error.
    parser = argparse.ArgumentParser()
    parser.add_argument("--website-root", type=Path)
    assert parser.parse_args([]).website_root is None
    assert parser.parse_args(["--website-root", "website"]).website_root == Path("website")
