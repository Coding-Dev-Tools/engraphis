import hashlib
import json
from pathlib import Path

import pytest

from scripts import render_external_diagnostics


def test_renderer_parses_the_exact_snapshot_it_verified(tmp_path, monkeypatch):
    analysis = tmp_path / "analysis.json"
    original = {"schema": "engraphis-external-analysis/v1", "reports": [{"recall": 0.25}]}
    payload = json.dumps(original).encode()
    digest = hashlib.sha256(payload).hexdigest()
    analysis.write_bytes(payload)
    analysis.with_suffix(".json.sha256").write_text(digest + "  analysis.json\n")
    read_bytes = Path.read_bytes

    def replace_after_read(path):
        captured = read_bytes(path)
        if path == analysis:
            path.write_text('{"schema":"replacement","reports":[{"recall":1.0}]}')
        return captured

    monkeypatch.setattr(Path, "read_bytes", replace_after_read)
    data, verified_digest = render_external_diagnostics._read_analysis(analysis)
    assert data == original
    assert verified_digest == digest
    assert json.loads(analysis.read_text())["schema"] == "replacement"


def test_renderer_rejects_unverified_analysis(tmp_path):
    analysis = tmp_path / "analysis.json"
    analysis.write_text('{"schema":"engraphis-external-analysis/v1","reports":[{}]}')
    analysis.with_suffix(".json.sha256").write_text("0" * 64)
    with pytest.raises(ValueError, match="checksum mismatch"):
        render_external_diagnostics._read_analysis(analysis)
