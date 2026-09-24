"""The PyPI audit may exclude only the exact unpublished installed candidate."""
from __future__ import annotations

import pytest

from scripts.pin_installed_audit_requirements import published_pins


def _distribution(site, directory: str, name: str, version: str) -> None:
    metadata = site / f"{directory}.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )


def test_exact_installed_dependencies_are_pinned_but_candidate_is_excluded(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    _distribution(site, "engraphis-1.7.6", "Engraphis", "1.7.6")
    _distribution(site, "numpy-2.4.6", "NumPy", "2.4.6")
    _distribution(site, "setuptools-84.0.0", "setuptools", "84.0.0")

    assert published_pins(site, candidate_name="engraphis", candidate_version="1.7.6") == [
        "numpy==2.4.6",
        "setuptools==84.0.0",
    ]


@pytest.mark.parametrize("problem", ["missing", "wrong-version", "duplicate", "missing-name"])
def test_candidate_or_metadata_mismatch_fails_closed(tmp_path, problem):
    site = tmp_path / "site-packages"
    site.mkdir()
    _distribution(site, "numpy-2.4.6", "numpy", "2.4.6")
    if problem != "missing":
        _distribution(
            site,
            "engraphis-1.7.5" if problem == "wrong-version" else "engraphis-1.7.6",
            "engraphis",
            "1.7.5" if problem == "wrong-version" else "1.7.6",
        )
    if problem == "duplicate":
        _distribution(site, "engraphis-extra", "Engraphis", "1.7.6")
    if problem == "missing-name":
        broken = site / "broken.dist-info"
        broken.mkdir()
        (broken / "METADATA").write_text(
            "Metadata-Version: 2.1\nVersion: 1.0\n", encoding="utf-8"
        )

    with pytest.raises(ValueError):
        published_pins(site, candidate_name="engraphis", candidate_version="1.7.6")


def test_duplicate_dependency_cannot_be_silently_dropped(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    _distribution(site, "engraphis-1.7.6", "engraphis", "1.7.6")
    _distribution(site, "first", "zope.interface", "6.0")
    _distribution(site, "second", "zope-interface", "6.1")

    with pytest.raises(ValueError, match="duplicate installed distribution"):
        published_pins(site, candidate_name="engraphis", candidate_version="1.7.6")
