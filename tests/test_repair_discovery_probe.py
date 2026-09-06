"""Failed probe attempts retain source identity instead of disappearing from evidence."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("backlog", [0, 5])
def test_probe_cli_retains_success_and_failure_artifacts(tmp_path, backlog):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "result.json"
    result = subprocess.run(
        [sys.executable, "-m", "eval.repair_discovery", "--backlog", str(backlog),
         "--output", str(output), "--temp-root", str(tmp_path)],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    report = json.loads(output.read_text())
    assert report["configuration"] == {"backlog": backlog, "repetition": 1}
    assert report["driver_sha256"] == hashlib.sha256(
        (root / "eval/repair_discovery.py").read_bytes(),
    ).hexdigest()
    assert report["source_files"]["engraphis/core/vector_repair.py"] == hashlib.sha256(
        (root / "engraphis/core/vector_repair.py").read_bytes(),
    ).hexdigest()
    if backlog:
        assert result.returncode == 0 and report["status"] == "ok"
        assert report["measurement"]["result"] == {"attempted": 1, "repaired": 1, "pending": 5}
        assert report["measurement"]["erasure_verified"] is True
    else:
        assert result.returncode != 0 and report["status"] == "failed"
        assert report["failure"]["type"] == "ValueError"
        assert "measurement" not in report
