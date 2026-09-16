"""Focused tests for the candidate-only coding-campaign oracle runner."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.campaign_oracle import (
    OracleError,
    OracleOperation,
    _RESULT_MARKER,
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

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        if command[1] == "run":
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=_RESULT_MARKER + '{"ok":true,"value":41}\n',
                stderr="",
            )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("eval.campaign_oracle.subprocess.run", fake_run)
    result = docker_oracle(scenario, workspace, "image@sha256:abc")

    assert result["passed"] is True
    assert len(calls) == 2
    operation_payload = "\n".join(calls[0][-3:])
    assert "41" not in operation_payload
    assert "/oracle.py" not in "\n".join(calls[0])
    assert calls[1][:4] == ["docker", "rm", "--force", calls[0][3]]


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
