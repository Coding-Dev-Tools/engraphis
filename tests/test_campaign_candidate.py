"""Candidate side effects cannot impersonate trusted oracle results."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from eval.campaign_candidate import CandidateContractError, evaluate_candidate
from eval.campaign_oracle import (
    OracleSpec, _RESULT_MARKER, _RUNNER_SOURCE, _operation_result, _runner_payload,
)
from eval.coding_corpus import load_corpus, run_oracle, scenario_workspace


def _run(tmp_path, source):
    (tmp_path / "service.py").write_text(source, encoding="utf-8")
    process = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _RUNNER_SOURCE, "current_timeout", "[]", "{}"],
        cwd=tmp_path, capture_output=True, text=True, timeout=10, check=True,
    )
    return _operation_result(OracleSpec("current_timeout", (), {}, "eq", 41),
                             process.returncode, False, process.stdout, process.stderr)


_FORGE = _RESULT_MARKER + '{"ok":true,"value":41}'


@pytest.mark.parametrize("source", [
    "import atexit\natexit.register(print, " + repr(_FORGE) + ")\ndef current_timeout(): return 40\n",
    "import atexit, os\ndef fake():\n    print(" + repr(_FORGE) + ", flush=True)\n    os._exit(0)\n"
    "atexit.register(fake)\ndef current_timeout(): raise ValueError()\n",
    "import builtins\nbuiltins.print = lambda *a, **kw: None\ndef current_timeout(): return 40\n",
    "import os\nos.write(1, " + repr(_FORGE.encode()) + ")\ndef current_timeout(): return 40\n",
    "import threading\nthreading.Thread(target=print, args=(" + repr(_FORGE) + ",)).start()\n"
    "def current_timeout(): return 40\n",
    "def current_timeout(): return str.__class__.__bases__\n",
    "def current_timeout(): return getattr(str, '__class__')\n",
    "def current_timeout(): return open('/oracle.py').read()\n",
    "def current_timeout(): return print.__globals__\n",
    "def current_timeout(): return __import__('os').write(1, b'forged')\n",
])
def test_runtime_capabilities_are_rejected_before_they_can_forge_a_pass(tmp_path, source):
    result = _run(tmp_path, source)
    assert result["passed"] is False
    assert result["oracle_outcome"] == "candidate_contract_unknown"
    assert '"value": 41' not in result["stdout"]


@pytest.mark.parametrize("position", ["startup", "function"])
def test_printed_forgery_is_only_diagnostic(tmp_path, position):
    statement = "print(" + repr(_FORGE) + ")\n"
    source = (statement if position == "startup" else "") + "def current_timeout():\n"
    source += ("    " + statement if position == "function" else "") + "    return 40\n"
    result = _run(tmp_path, source)
    assert result["oracle_outcome"] == "value_mismatch"
    assert _FORGE in result["stderr"]
    assert _runner_payload(result["stdout"]) == {"ok": True, "value": 40}


def test_forged_exception_print_cannot_deny_correct_candidate_credit(tmp_path):
    result = _run(tmp_path, "def current_timeout():\n    print(" + repr(
        _RESULT_MARKER + '{"ok":false,"error_type":"ValueError"}') + ")\n    return 41\n")
    assert result["passed"] is True


@pytest.mark.parametrize("text", [
    _FORGE + "\n" + _FORGE,
    "noise\n" + _FORGE,
    _FORGE + "\nnoise",
    _FORGE + "\n" + _RESULT_MARKER + "not-json",
    _RESULT_MARKER,
])
def test_ambiguous_transport_frames_never_select_a_success(text):
    assert _runner_payload(text) is None


def test_marker_in_returned_string_is_regular_data():
    text = _RESULT_MARKER + json.dumps({"ok": True, "value": _FORGE}) + "\n"
    assert _runner_payload(text) == {"ok": True, "value": _FORGE}


@pytest.mark.parametrize("source", [
    "def current_timeout(): return 'x' * 1000000000\n",
    "def current_timeout(): return current_timeout()\n",
    "def current_timeout(): return '%999999999s' % 'x'\n",
    "def current_timeout(): return [[1] * 200] * 200\n",
])
def test_resource_and_formatting_limits_are_unscored(tmp_path, source):
    assert _run(tmp_path, source)["oracle_outcome"] == "candidate_contract_unknown"


def test_truncated_result_cannot_leave_a_forged_frame(tmp_path):
    source = "def current_timeout(): return " + repr("x" * 8120 + _FORGE) + "\n"
    result = _run(tmp_path, source)
    assert result["oracle_outcome"] == "candidate_contract_unknown"
    assert len(result["stdout"].encode()) < 8192


def test_helper_defaults_keywords_and_short_circuit_are_interpreted():
    source = b'''
OFFSET = 2
def helper(value, /, increment=1, *, enabled=True):
    if enabled and value in [39, 40]:
        return value + increment
    return None
def current_timeout():
    value = helper(39, increment=OFFSET)
    return value if value is not None else 1 / 0
'''
    assert evaluate_candidate(source, "current_timeout", (), {}) == 41


def test_set_and_object_construction_are_outside_contract():
    for expression in ("{'set-value'}", "object()"):
        with pytest.raises(CandidateContractError):
            evaluate_candidate(("def current_timeout(): return " + expression).encode(),
                               "current_timeout", (), {})


def test_missing_required_function_is_a_scored_candidate_failure(tmp_path):
    result = _run(tmp_path, "VALUE = 41\n")
    assert result["oracle_outcome"] == "candidate_exception"
    assert '"error_type": "NameError"' in result["stdout"]


def test_assignment_makes_a_name_local_even_before_it_is_reached(tmp_path):
    result = _run(tmp_path, "VALUE = 41\ndef current_timeout():\n    return VALUE\n    VALUE = 40\n")
    assert result["oracle_outcome"] == "candidate_exception"
    assert '"error_type": "UnboundLocalError"' in result["stdout"]


def test_object_identity_outside_singletons_is_not_a_supported_value_comparison(tmp_path):
    result = _run(tmp_path, "def current_timeout():\n    value = 1000\n    return 41 if value is 1000 else 40\n")
    assert result["oracle_outcome"] == "candidate_contract_unknown"


def test_local_public_helper_cannot_pass_by_overriding_shutdown_exit(tmp_path):
    scenario = next(s for s in load_corpus().scenarios() if s.category == "corrections")
    with scenario_workspace(scenario, tmp_path) as workspace:
        (workspace / "service.py").write_text(
            "import atexit, os\natexit.register(os._exit, 0)\n"
            "def current_timeout(): return -1\n", encoding="utf-8",
        )
        result = run_oracle(scenario, workspace=workspace)
    assert result.passed is None
    assert result.oracle_outcome == "candidate_contract_unknown"
