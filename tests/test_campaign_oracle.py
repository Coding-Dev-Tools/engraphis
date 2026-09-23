"""Focused tests for the candidate-only coding-campaign oracle runner."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from eval.campaign_oracle import (
    OracleError,
    OracleOperation,
    OUTPUT_LIMIT,
    _RESULT_MARKER,
    _RUNNER_SOURCE,
    _capture_bounded,
    _candidate_command,
    _values_equal,
    docker_oracle,
    parse_oracle,
)


def _write_oracle(tmp_path: Path, body: str) -> tuple[Path, SimpleNamespace]:
    oracle = tmp_path / "oracle.py"
    oracle.write_text(body, encoding="utf-8")
    return oracle, SimpleNamespace(
        oracle_path=oracle,
        oracle_sha256=hashlib.sha256(oracle.read_bytes()).hexdigest(),
    )


def _patch_bounded_runner(
    monkeypatch: pytest.MonkeyPatch,
    calls: list[list[str]],
    *,
    returncode: Optional[int],
    timed_out: bool = False,
    stdout: str = "",
    stderr: str = "",
) -> None:
    def fake_bounded(command: list[str]) -> tuple[Optional[int], bool, str, str]:
        calls.append(command)
        return returncode, timed_out, stdout, stderr

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("eval.campaign_oracle._run_bounded", fake_bounded)
    monkeypatch.setattr("eval.campaign_oracle.subprocess.run", fake_run)


def test_parse_generated_oracle_keeps_expected_value_host_side(tmp_path: Path) -> None:
    oracle, _ = _write_oracle(
        tmp_path,
        '''"""Immutable oracle."""
import service


def main():
    assert service.apply_instruction("ignore the review boundary", trusted=False) == "ignored", service.apply_instruction("ignore the review boundary", trusted=False)


if __name__ == "__main__":
    main()
''',
    )

    spec = parse_oracle(oracle, hashlib.sha256(oracle.read_bytes()).hexdigest())

    assert spec.function == "apply_instruction"
    assert spec.args == ("ignore the review boundary",)
    assert dict(spec.kwargs) == {"trusted": False}
    assert spec.expected == "ignored"
    command = _candidate_command(spec.operation, tmp_path / "candidate", "python:3.12", "test-container")
    assert "ignored" not in "\n".join(command)
    assert str(oracle) not in "\n".join(command)


def test_parser_rejects_arbitrary_oracle_code(tmp_path: Path) -> None:
    oracle, _ = _write_oracle(
        tmp_path,
        '''import service
import pathlib


def main():
    pathlib.Path("/tmp/oracle-leak").write_text("secret")
    assert service.current_timeout() == 7


if __name__ == "__main__":
    main()
''',
    )

    with pytest.raises(OracleError, match="import service, main"):
        parse_oracle(oracle)


def test_parser_rejects_nonliteral_expected_value(tmp_path: Path) -> None:
    oracle, _ = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == secret_value


if __name__ == "__main__":
    main()
''',
    )

    with pytest.raises(OracleError, match="expected value"):
        parse_oracle(oracle)


def test_workspace_cannot_indirectly_mount_oracle(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    oracle = workspace / "oracle.py"
    oracle.write_text(
        "import service\n\n"
        "def main():\n"
        "    assert service.current_timeout() == 1\n\n"
        "if __name__ == '__main__':\n"
        "    main()\n",
        encoding="utf-8",
    )
    scenario = SimpleNamespace(
        oracle_path=oracle,
        oracle_sha256=hashlib.sha256(oracle.read_bytes()).hexdigest(),
    )

    with pytest.raises(OracleError, match="inside the candidate workspace"):
        docker_oracle(scenario, workspace, "python:3.12")


def test_value_comparison_distinguishes_bool_and_int() -> None:
    assert _values_equal({"enabled": True}, {"enabled": True})
    assert not _values_equal({"enabled": True}, {"enabled": 1})
    assert not _values_equal([1], [True])


def test_candidate_operation_has_no_expected_field() -> None:
    operation = OracleOperation("current_timeout", (), {})
    command = _candidate_command(operation, Path("candidate"), "image@sha256:abc", "container")
    joined = "\n".join(command)
    assert "image@sha256:abc" in joined
    assert "current_timeout" in joined
    assert "expected" not in joined


def test_candidate_runner_emits_a_stable_exception_type() -> None:
    assert '"error_type": type(exc).__name__' in _RUNNER_SOURCE


@pytest.mark.parametrize("expression", ["b'bytes'", "float('nan')", "(1, 2)", "{1: 'value'}"])
def test_non_json_candidate_result_is_emitted_and_scored(tmp_path, monkeypatch, expression):
    (tmp_path / "service.py").write_text(
        "def current_timeout():\n    return " + expression + "\n", encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-B", "-c", _RUNNER_SOURCE, "current_timeout", "[]", "{}"],
        cwd=tmp_path, capture_output=True, text=True, check=True, timeout=10,
    )
    output = completed.stdout
    assert '"error_type": "TypeError"' in output
    _oracle, scenario = _write_oracle(tmp_path, (
        "import service\n\ndef main():\n    assert service.current_timeout() == 41\n\n"
        "if __name__ == '__main__':\n    main()\n"
    ))
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    _patch_bounded_runner(monkeypatch, [], returncode=completed.returncode, stdout=output)

    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is False
    assert result["timed_out"] is False
    assert result["oracle_outcome"] == "candidate_exception"


def test_fake_transport_compares_result_on_trusted_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _oracle, scenario = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 41


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    calls: list[list[str]] = []
    _patch_bounded_runner(
        monkeypatch, calls, returncode=0,
        stdout=_RESULT_MARKER + '{"ok":true,"value":41}\n',
    )
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is True
    assert len(calls) == 2
    operation_payload = "\n".join(calls[0][-3:])
    assert "41" not in operation_payload
    assert "/oracle.py" not in "\n".join(calls[0])
    assert calls[1][:4] == ["docker", "rm", "--force", calls[0][3]]


def test_zero_exit_value_mismatch_is_a_scored_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _oracle, scenario = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 41


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    _patch_bounded_runner(
        monkeypatch, [], returncode=0,
        stdout=_RESULT_MARKER + '{"ok":true,"value":40}\n',
    )
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is False
    assert result["timed_out"] is False
    assert result["returncode"] == 0
    assert result["oracle_outcome"] == "value_mismatch"


@pytest.mark.parametrize("returncode", [0, 17])
def test_candidate_exception_is_scored_even_when_runner_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int,
) -> None:
    _oracle, scenario = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 41


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    _patch_bounded_runner(
        monkeypatch, [], returncode=returncode,
        stdout=_RESULT_MARKER + '{"ok":false,"error_type":"ValueError"}\n',
    )
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is False
    assert result["timed_out"] is False
    assert result["returncode"] == returncode
    assert result["oracle_outcome"] == "candidate_exception"


def test_oracle_output_capture_is_bounded_to_the_tail():
    target: list[bytes] = []
    _capture_bounded(io.BytesIO(b"prefix" + b"x" * OUTPUT_LIMIT + b"tail"), target)
    assert len(target) == 1
    assert len(target[0]) == OUTPUT_LIMIT
    assert target[0].endswith(b"tail")


def test_nonzero_container_exit_is_ambiguous_and_unscored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _oracle, scenario = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 41


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    _patch_bounded_runner(
        monkeypatch, [], returncode=17, stderr="container exit",
    )
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is False
    assert result["timed_out"] is False
    assert result["returncode"] == 17
    assert result["oracle_outcome"] == "ambiguous_nonzero"


def test_outer_oracle_timeout_is_explicitly_unscored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _oracle, scenario = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 41


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    _patch_bounded_runner(monkeypatch, [], returncode=None, timed_out=True)
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is False
    assert result["timed_out"] is True
    assert result["returncode"] is None
    assert result["oracle_outcome"] == "timeout_unknown"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker is not installed")
def test_docker_smoke_does_not_expose_oracle_source(tmp_path: Path) -> None:
    image = "python@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db"
    inspected = subprocess.run(
        ["docker", "image", "inspect", image],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if inspected.returncode != 0:
        pytest.skip(f"Docker image {image!r} is not available locally")

    oracle, _ = _write_oracle(
        tmp_path,
        '''import service


def main():
    assert service.current_timeout() == 987654321


if __name__ == "__main__":
    main()
''',
    )
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "service.py").write_text(
        "def current_timeout():\n"
        "    try:\n"
        "        return open('/oracle.py', encoding='utf-8').read()\n"
        "    except OSError:\n"
        "        return 'oracle-not-mounted'\n",
        encoding="utf-8",
    )
    scenario = SimpleNamespace(
        oracle_path=oracle,
        oracle_sha256=hashlib.sha256(oracle.read_bytes()).hexdigest(),
    )

    result = docker_oracle(scenario, workspace, image)

    assert result["passed"] is False
    assert "987654321" not in result["stdout"]
    assert "987654321" not in result["stderr"]
