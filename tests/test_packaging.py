from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_distribution_configuration_excludes_runtime_bytecode():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "include-package-data = false" in pyproject
    assert '"*" = ["*.pyc", "*.pyo", "__pycache__/*"]' in pyproject
    assert "global-exclude *.pyc" in manifest
    assert "global-exclude *.pyo" in manifest
