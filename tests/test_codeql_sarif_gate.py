from __future__ import annotations

import json

from scripts.check_codeql_sarif import MAX_REPORTED_FINDINGS, findings_in, main


def _write_sarif(tmp_path, results):
    path = tmp_path / "python.sarif"
    path.write_text(
        json.dumps({"version": "2.1.0", "runs": [{"results": results}]}),
        encoding="utf-8",
    )
    return path


def test_codeql_gate_accepts_clean_sarif(tmp_path, capsys) -> None:
    _write_sarif(tmp_path, [])

    assert main([str(tmp_path)]) == 0
    assert "CodeQL gate: clean" in capsys.readouterr().out


def test_codeql_gate_reports_and_rejects_findings(tmp_path, capsys) -> None:
    path = _write_sarif(
        tmp_path,
        [
            {
                "ruleId": "py/example",
                "message": {"text": "unsafe example"},
                "locations": [
                    {
                        "physicalLocation": {
                            "artifactLocation": {"uri": "engraphis/example.py"},
                            "region": {"startLine": 12},
                        }
                    }
                ],
            }
        ],
    )

    assert findings_in(path) == [
        "py/example at engraphis/example.py:12: unsafe example"
    ]
    assert main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "CodeQL gate: 1 finding(s)" in captured.err
    assert "py/example at engraphis/example.py:12" in captured.err


def test_codeql_gate_rejects_missing_sarif(tmp_path, capsys) -> None:
    assert main([str(tmp_path)]) == 2
    assert "no SARIF files found" in capsys.readouterr().err


def test_codeql_gate_bounds_finding_output(tmp_path, capsys) -> None:
    _write_sarif(
        tmp_path,
        [
            {
                "ruleId": f"py/example-{index}",
                "message": {"text": "unsafe example"},
            }
            for index in range(MAX_REPORTED_FINDINGS + 1)
        ],
    )

    assert main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert f"CodeQL gate: {MAX_REPORTED_FINDINGS + 1} finding(s)" in captured.err
    assert "- ... 1 additional finding(s) omitted" in captured.err
