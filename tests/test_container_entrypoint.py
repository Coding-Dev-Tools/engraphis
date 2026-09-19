"""Exercise the actual POSIX path validator without running privileged startup."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"),
                                reason="POSIX path and symlink semantics required")


def _validate(path: str) -> int:
    entrypoint = (Path(__file__).resolve().parents[1] / "docker-entrypoint.sh").read_text()
    body = entrypoint.split("    reject_linked_path() {", 1)[1].split("\n    }", 1)[0]
    script = 'reject_linked_path() {' + body + '\n}\nreject_linked_path "$1"\n'
    return subprocess.run(["sh", "-c", script, "validator", path], check=False).returncode


@pytest.mark.parametrize("path", ["relative/config.env", "../config.env", "/tmp/../etc/config.env"])
def test_root_path_validation_rejects_relative_and_parent_traversal(path):
    assert _validate(path) != 0


def test_root_path_validation_checks_intermediate_symlinks_before_dot_segments(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "nested").mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    assert _validate(str(link / "nested" / "config.env")) != 0
    assert _validate(str(link) + "/../config.env") != 0
    assert _validate(str(target / "nested" / "config.env")) == 0
    assert _validate(str(tmp_path / "new" / "config.env")) == 0
