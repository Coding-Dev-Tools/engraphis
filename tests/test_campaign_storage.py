from types import SimpleNamespace

import pytest

from eval import campaign_storage as storage


def test_graph_store_owns_only_its_new_loopback_container(monkeypatch):
    calls = []
    monkeypatch.setattr(storage.subprocess, "run", lambda args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0))
    with pytest.raises(RuntimeError):
        with storage.graph_store(True):
            raise RuntimeError("interrupted")
    run = calls[1]
    assert "127.0.0.1:17687:7687" in run
    assert storage.NEO4J_IMAGE in run
    assert "--volume" not in run
    assert calls[-1] == ["docker", "rm", "--force", run[run.index("--name") + 1]]


def test_disabled_graph_storage_does_not_touch_docker(monkeypatch):
    monkeypatch.setattr(storage.subprocess, "run", lambda *a, **k: pytest.fail("unexpected Docker"))
    with storage.graph_store(False):
        pass
