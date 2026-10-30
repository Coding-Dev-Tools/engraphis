"""Status publication tolerates transient readers without hiding persistent denials."""
import json

import pytest

from eval import local_benchmark_queue as queue


def test_atomic_status_replacement_retries_transient_reader_locks(monkeypatch, tmp_path):
    replace = queue.os.replace
    attempts = []
    delays = []

    def busy(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError("reader still active")
        replace(source, target)

    monkeypatch.setattr(queue, "_STATUS_REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(queue.os, "replace", busy)
    monkeypatch.setattr(queue.time, "sleep", delays.append)
    queue._status(tmp_path, status="COMPLETE")
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "COMPLETE"
    assert len(attempts) == 3
    assert len(delays) == 2


def test_persistent_status_denial_is_bounded_and_preserves_the_prior_status(monkeypatch, tmp_path):
    queue._status(tmp_path, status="PRIOR")
    attempts = []

    def denied(*args):
        attempts.append(args)
        raise PermissionError("persistent access denial")

    monkeypatch.setattr(queue, "_STATUS_REPLACE_ATTEMPTS", 3)
    monkeypatch.setattr(queue.os, "replace", denied)
    monkeypatch.setattr(queue.time, "sleep", lambda duration: None)
    with pytest.raises(PermissionError, match="persistent"):
        queue._status(tmp_path, status="NEW")
    assert len(attempts) == 3
    assert json.loads((tmp_path / "status.json").read_text())["status"] == "PRIOR"
