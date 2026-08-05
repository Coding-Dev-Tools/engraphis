import os
import re
import subprocess
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import smoke_entry_points


def _wrappers(tmp_path):
    """Create placeholder generated wrappers for the injected runner tests."""
    suffix = ".exe" if os.name == "nt" else ""
    for name in smoke_entry_points.EXPECTED_ENTRY_POINTS:
        (tmp_path / (name + suffix)).touch()


def _pyproject_scripts() -> dict[str, str]:
    """Read the deliberately simple [project.scripts] mapping without a test dependency."""
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    section = text.split("[project.scripts]", 1)[1].split("[tool.setuptools]", 1)[0]
    return {
        match.group(1): match.group(2)
        for match in re.finditer(
            r'^([A-Za-z0-9_-]+)\s*=\s*"([^"]+)"\s*$', section, re.MULTILINE
        )
    }


def test_entry_point_manifest_matches_pyproject():
    assert smoke_entry_points.EXPECTED_ENTRY_POINTS == _pyproject_scripts()


def test_smoke_runs_every_expected_wrapper_without_path_lookup(tmp_path):
    suffix = ".exe" if os.name == "nt" else ""
    _wrappers(tmp_path)
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="usage: synthetic\n", stderr="")

    passed = smoke_entry_points.smoke_entry_points(
        entries=smoke_entry_points.EXPECTED_ENTRY_POINTS,
        scripts_dir=tmp_path,
        timeout=1,
        runner=runner,
    )

    assert passed == sorted(smoke_entry_points.EXPECTED_ENTRY_POINTS)
    assert [Path(command[0]).name for command, _kwargs in calls] == [
        name + suffix for name in sorted(smoke_entry_points.EXPECTED_ENTRY_POINTS)
    ]
    assert all(command[1] == "--help" for command, _kwargs in calls)
    assert all(kwargs["stdin"] is subprocess.DEVNULL for _command, kwargs in calls)


def test_smoke_rejects_missing_or_changed_installed_metadata(tmp_path):
    with pytest.raises(RuntimeError, match="missing=engraphis-update"):
        smoke_entry_points.smoke_entry_points(
            entries={
                name: target
                for name, target in smoke_entry_points.EXPECTED_ENTRY_POINTS.items()
                if name != "engraphis-update"
            },
            scripts_dir=tmp_path,
        )

    mismatched = dict(smoke_entry_points.EXPECTED_ENTRY_POINTS)
    mismatched["engraphis"] = "scripts.cli:main"
    with pytest.raises(RuntimeError, match="target mismatch=engraphis"):
        smoke_entry_points.smoke_entry_points(entries=mismatched, scripts_dir=tmp_path)


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_smoke_rejects_non_finite_or_non_positive_timeout(timeout):
    with pytest.raises(ValueError, match="positive finite"):
        smoke_entry_points.smoke_entry_points(entries={}, timeout=timeout)


def test_smoke_rejects_a_wrapper_without_usage_text(tmp_path):
    _wrappers(tmp_path)

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="ready\n", stderr="")

    with pytest.raises(RuntimeError, match="produced no usage text"):
        smoke_entry_points.smoke_entry_points(
            entries=smoke_entry_points.EXPECTED_ENTRY_POINTS,
            scripts_dir=tmp_path,
            timeout=1,
            runner=runner,
        )


def test_smoke_reports_a_timed_out_wrapper(tmp_path):
    _wrappers(tmp_path)

    def runner(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    with pytest.raises(RuntimeError, match=r"engraphis --help timed out after 1\.0s"):
        smoke_entry_points.smoke_entry_points(
            entries=smoke_entry_points.EXPECTED_ENTRY_POINTS,
            scripts_dir=tmp_path,
            timeout=1,
            runner=runner,
        )


def test_smoke_bounds_nonzero_wrapper_diagnostics(tmp_path):
    _wrappers(tmp_path)
    oversized = "A" * (smoke_entry_points._OUTPUT_LIMIT + 1) + "SECRET-TAIL"

    def runner(command, **_kwargs):
        return subprocess.CompletedProcess(command, 17, stdout=oversized, stderr="fatal")

    with pytest.raises(RuntimeError) as exc_info:
        smoke_entry_points.smoke_entry_points(
            entries=smoke_entry_points.EXPECTED_ENTRY_POINTS,
            scripts_dir=tmp_path,
            timeout=1,
            runner=runner,
        )

    message = str(exc_info.value)
    assert "engraphis --help exited 17" in message
    assert "stdout:" in message and "stderr:\nfatal" in message
    assert "[truncated]" in message
    assert "SECRET-TAIL" not in message


def test_installed_entry_points_supports_python39_metadata_and_filters_others(monkeypatch):
    points = [
        SimpleNamespace(name="engraphis", value="scripts.entry:main", group="console_scripts"),
        SimpleNamespace(name="engraphis-extra", value="pkg:main", group="console_scripts"),
        SimpleNamespace(name="other-tool", value="pkg:main", group="console_scripts"),
        SimpleNamespace(name="engraphis-api", value="pkg:main", group="not-console"),
    ]
    monkeypatch.setattr(
        smoke_entry_points.importlib.metadata,
        "distribution",
        lambda _distribution: SimpleNamespace(entry_points=points),
    )

    assert smoke_entry_points.installed_entry_points() == {
        "engraphis": "scripts.entry:main",
        "engraphis-extra": "pkg:main",
    }
