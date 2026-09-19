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


def test_external_state_requires_an_existing_app_owned_directory(tmp_path):
    entrypoint = (Path(__file__).resolve().parents[1] / "docker-entrypoint.sh").read_text()
    body = entrypoint.split("    state_directory_is_owned() {", 1)[1].split("\n    }", 1)[0]
    script = 'state_directory_is_owned() {' + body + '\n}\nstate_directory_is_owned "$1" "$2"\n'
    owner = tmp_path.stat().st_uid

    def check(path, uid):
        return subprocess.run(["sh", "-c", script, "validator", str(path), str(uid)],
                              check=False).returncode

    assert check(tmp_path, owner) == 0
    assert check(tmp_path, owner + 1) != 0
    assert check(tmp_path / "missing", owner) != 0
    assert check("/data/new-state", owner) == 0


def test_external_state_marker_cannot_skip_repair_of_a_replaced_volume(tmp_path):
    managed = tmp_path / "data"
    external = tmp_path / "external-state"
    binaries = tmp_path / "bin"
    for directory in (managed, external, binaries):
        directory.mkdir()
    legacy_marker = external / ".volume-ownership"
    legacy_marker.write_text("older external volume")
    log = tmp_path / "chown.log"
    shims = {
        "id": 'case "$*" in "-u engraphis") printf "%s\\n" "$APP_UID";; *) echo 0;; esac\n',
        "chown": 'printf "%s\\n" "$*" >> "$CHOWN_LOG"\n',
        "gosu": 'shift\nexec "$@"\n',
    }
    for name, body in shims.items():
        executable = binaries / name
        executable.write_text("#!/bin/sh\n" + body)
        executable.chmod(0o755)
    entrypoint = (Path(__file__).resolve().parents[1] / "docker-entrypoint.sh").read_text()
    # Remap only the managed volume in this unprivileged startup exercise.
    entrypoint = entrypoint.replace('ownership_marker="/data/.volume-ownership"',
                                    'ownership_marker="$MANAGED_VOLUME/.volume-ownership"')
    entrypoint = entrypoint.replace('chown -R engraphis:engraphis /data',
                                    'chown -R engraphis:engraphis "$MANAGED_VOLUME"')
    entrypoint = entrypoint.replace('chown engraphis:engraphis /data',
                                    'chown engraphis:engraphis "$MANAGED_VOLUME"')
    script = tmp_path / "entrypoint.sh"
    script.write_text(entrypoint)
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
           "APP_UID": str(external.stat().st_uid), "CHOWN_LOG": str(log),
           "MANAGED_VOLUME": str(managed), "ENGRAPHIS_STATE_DIR": str(external),
           "ENGRAPHIS_ENV_FILE": ""}
    subprocess.run(["sh", str(script), "true"], env=env, check=True)
    assert f"-R engraphis:engraphis {managed}" in log.read_text().splitlines()
    assert (managed / ".volume-ownership").is_file()
    assert legacy_marker.read_text() == "older external volume"

    log.write_text("")
    subprocess.run(["sh", str(script), "true"], env=env, check=True)
    assert not any(line.startswith("-R ") for line in log.read_text().splitlines())
