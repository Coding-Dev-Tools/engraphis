from __future__ import annotations

import json
from pathlib import Path

from scripts.check_codeql_sarif import MAX_REPORTED_FINDINGS, findings_in, main


def _write_sarif(tmp_path, results):
    path = tmp_path / "python.sarif"
    path.write_text(
        json.dumps({"version": "2.1.0", "runs": [{"results": results}]}),
        encoding="utf-8",
    )
    return path


def _weak_hash_result(uri: str, line: int, message: str) -> dict:
    return {
        "ruleId": "py/weak-sensitive-data-hashing",
        "message": {"text": message},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": uri},
                    "region": {"startLine": line},
                },
            },
        ],
    }


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


def test_codeql_gate_reports_path_problem_endpoints(tmp_path) -> None:
    path = _write_sarif(
        tmp_path,
        [{
            "ruleId": "py/example",
            "message": {"text": "unsafe example"},
            "locations": [],
            "codeFlows": [{"threadFlows": [{"locations": [
                {"location": {"physicalLocation": {
                    "artifactLocation": {"uri": "source.py"}, "region": {"startLine": 4},
                }}},
                {"location": {"physicalLocation": {
                    "artifactLocation": {"uri": "sink.py"}, "region": {"startLine": 9},
                }}},
            ]}]}],
        }],
    )

    assert findings_in(path) == [
        "py/example at <unknown>: unsafe example [flow: source.py:4 -> sink.py:9]"
    ]


def test_codeql_gate_waives_only_the_two_exact_nonsecurity_hash_calls(tmp_path) -> None:
    path = _write_sarif(
        tmp_path,
        [
            _weak_hash_result(
                "file:///github/workspace/engraphis/backends/embedder_deterministic.py",
                36,
                "approved embedder hash",
            ),
            _weak_hash_result(
                r".\engraphis\backends\codegraph.py",
                182,
                "approved code hash",
            ),
            _weak_hash_result(
                "engraphis/backends/embedder_deterministic.py",
                37,
                "wrong line",
            ),
            _weak_hash_result("engraphis/security.py", 36, "wrong file"),
        ],
    )

    assert findings_in(path) == [
        "py/weak-sensitive-data-hashing at "
        "engraphis/backends/embedder_deterministic.py:37: wrong line",
        "py/weak-sensitive-data-hashing at engraphis/security.py:36: wrong file",
    ]


def test_codeql_gate_does_not_waive_ambiguous_multilocation_result(tmp_path) -> None:
    result = _weak_hash_result(
        "engraphis/backends/embedder_deterministic.py",
        36,
        "ambiguous",
    )
    result["locations"].append(result["locations"][0])
    path = _write_sarif(tmp_path, [result])

    assert findings_in(path) == [
        "py/weak-sensitive-data-hashing at "
        "engraphis/backends/embedder_deterministic.py:36: ambiguous",
    ]


def test_codeql_query_remains_enabled_globally() -> None:
    config = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "codeql"
        / "codeql-config.yml"
    ).read_text(encoding="utf-8")

    assert "query-filters:" not in config
    assert "py/weak-sensitive-data-hashing" not in config


def test_codeql_gate_rejects_baselined_and_source_suppressed_findings(tmp_path, capsys) -> None:
    _write_sarif(
        tmp_path,
        [
            {
                "ruleId": "py/baselined-example",
                "baselineState": "unchanged",
                "message": {"text": "existing finding"},
            },
            {
                "ruleId": "py/source-suppressed-example",
                "suppressions": [{"kind": "inSource"}],
                "message": {"text": "suppressed finding"},
            },
        ],
    )

    # Count every raw result. Do not inherit pull-request baselining or silently
    # ignore a SARIF suppression.
    assert main([str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "CodeQL gate: 2 finding(s)" in captured.err
    assert "py/baselined-example" in captured.err
    assert "py/source-suppressed-example" in captured.err


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
