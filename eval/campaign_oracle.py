"""Isolated evaluation of coding-corpus oracle checks.

The coding campaign gives a model a disposable repository containing its edited
``service.py``.  The expected answer must stay outside that repository and
outside the candidate process.  This module therefore treats the generated
oracle as a small, checksummed declarative document: it parses the one supported
assertion on the trusted host, sends only the candidate operation to a locked
down container, and compares the returned value on the host.

This deliberately rejects arbitrary oracle Python.  A future oracle shape must
be added to this parser and covered by tests before it can run.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


MAX_ORACLE_BYTES = 64 * 1024
MAX_ORACLE_AST_NODES = 256
OUTPUT_LIMIT = 8192
ORACLE_TIMEOUT_SECONDS = 45

# These are the methods emitted by eval.coding_corpus._oracle_source.  Keeping
# this allowlist narrow means a malformed or hand-edited oracle cannot turn the
# evaluator into an arbitrary host-side Python runner.
ALLOWED_FUNCTIONS = frozenset(
    {
        "current_timeout",
        "current_policy",
        "scope_owner",
        "lookup_paraphrase",
        "relationship_target",
        "answer_unsupported",
        "apply_instruction",
        "retry_budget",
        "find_section",
        "localized_status",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESULT_MARKER = "__ENGRAPHIS_ORACLE_RESULT__"
_RUNNER_SOURCE = r'''
import importlib
import json
import sys

function_name = sys.argv[1]
args = json.loads(sys.argv[2])
kwargs = json.loads(sys.argv[3])
sys.path.insert(0, "/work")

try:
    module = importlib.import_module("service")
    value = getattr(module, function_name)(*args, **kwargs)
    payload = {"ok": True, "value": value}
except BaseException as exc:
    payload = {"ok": False, "error_type": type(exc).__name__}
    print("__ENGRAPHIS_ORACLE_RESULT__" + json.dumps(payload, sort_keys=True), flush=True)
    raise
else:
    print("__ENGRAPHIS_ORACLE_RESULT__" + json.dumps(payload, sort_keys=True), flush=True)
'''.strip()


class OracleError(ValueError):
    """Raised when an oracle is not in the bounded declarative format."""


@dataclass(frozen=True)
class OracleOperation:
    """The candidate-only part of an oracle check."""

    function: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]


@dataclass(frozen=True)
class OracleSpec:
    """A parsed check, including the host-only expected value."""

    function: str
    args: tuple[Any, ...]
    kwargs: Mapping[str, Any]
    operator: str
    expected: Any

    @property
    def operation(self) -> OracleOperation:
        """Return the operation that may cross the candidate boundary."""

        return OracleOperation(self.function, self.args, self.kwargs)


def _literal(node: ast.AST, label: str) -> Any:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise OracleError(f"oracle {label} must be a literal") from exc
    if not _is_json_value(value):
        raise OracleError(f"oracle {label} must be JSON-compatible")
    return value


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        # JSON has no non-finite number representation.  ``json.dumps`` with
        # allow_nan=False below gives the final check for floats.
        if isinstance(value, float):
            return value == value and value not in (float("inf"), float("-inf"))
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False


def _json(value: Any, label: str) -> str:
    if not _is_json_value(value):
        raise OracleError(f"oracle {label} must be JSON-compatible")
    try:
        return json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise OracleError(f"oracle {label} cannot be serialized") from exc


def _call(node: ast.AST, label: str) -> OracleOperation:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        raise OracleError(f"oracle {label} must call service.<allowed_method>")
    if not isinstance(node.func.value, ast.Name) or node.func.value.id != "service":
        raise OracleError(f"oracle {label} must call service.<allowed_method>")
    function = node.func.attr
    if function not in ALLOWED_FUNCTIONS or not _IDENTIFIER.fullmatch(function):
        raise OracleError(f"oracle method is not allowed: {function!r}")
    if any(isinstance(argument, ast.Starred) for argument in node.args):
        raise OracleError(f"oracle {label} cannot unpack positional arguments")
    if any(keyword.arg is None for keyword in node.keywords):
        raise OracleError(f"oracle {label} cannot unpack keyword arguments")
    args = tuple(_literal(argument, f"{label} argument") for argument in node.args)
    kwargs = {
        str(keyword.arg): _literal(keyword.value, f"{label} keyword")
        for keyword in node.keywords
    }
    # A duplicate keyword is rejected by Python itself, but rejecting it here
    # keeps the structured operation unambiguous even if the AST is built by a
    # caller rather than parsed from source.
    if len(kwargs) != len(node.keywords):
        raise OracleError(f"oracle {label} contains duplicate keyword arguments")
    return OracleOperation(function=function, args=args, kwargs=kwargs)


def _same_operation(left: OracleOperation, right: OracleOperation) -> bool:
    return left.function == right.function and left.args == right.args and dict(left.kwargs) == dict(right.kwargs)


def _module_docstring(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def parse_oracle(path: Path, expected_sha256: Optional[str] = None) -> OracleSpec:
    """Parse and validate one generated oracle without executing it."""

    path = Path(path)
    if path.is_symlink():
        raise OracleError("oracle symlinks are not accepted")
    try:
        source = path.read_bytes()
    except OSError as exc:
        raise OracleError(f"cannot read oracle: {path}") from exc
    if len(source) > MAX_ORACLE_BYTES:
        raise OracleError("oracle is too large")
    if expected_sha256 is not None:
        if not isinstance(expected_sha256, str):
            raise OracleError("oracle digest must be a string")
        actual_sha256 = hashlib.sha256(source).hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            raise OracleError("oracle changed after corpus load")
    try:
        tree = ast.parse(source.decode("utf-8"), filename=str(path), mode="exec")
    except (UnicodeDecodeError, SyntaxError, ValueError, MemoryError, RecursionError) as exc:
        raise OracleError("oracle is not valid UTF-8 Python") from exc
    if sum(1 for _ in ast.walk(tree)) > MAX_ORACLE_AST_NODES:
        raise OracleError("oracle syntax tree is too large")

    body = list(tree.body)
    if body and _module_docstring(body[0]):
        body.pop(0)
    if len(body) != 3:
        raise OracleError("oracle must contain import service, main, and a main guard")
    import_node, function_node, guard_node = body
    if (
        not isinstance(import_node, ast.Import)
        or len(import_node.names) != 1
        or import_node.names[0].name != "service"
        or import_node.names[0].asname is not None
    ):
        raise OracleError("oracle may import only the service module")
    if (
        not isinstance(function_node, ast.FunctionDef)
        or function_node.name != "main"
        or function_node.decorator_list
        or function_node.args.args
        or function_node.args.posonlyargs
        or function_node.args.kwonlyargs
        or function_node.args.vararg is not None
        or function_node.args.kwarg is not None
        or function_node.returns is not None
        or function_node.body is None
        or len(function_node.body) != 1
        or not isinstance(function_node.body[0], ast.Assert)
    ):
        raise OracleError("oracle main must contain exactly one assertion")
    if not isinstance(guard_node, ast.If) or not _is_main_guard(guard_node) or len(guard_node.body) != 1:
        raise OracleError("oracle must use a simple __main__ guard")
    call_main = guard_node.body[0]
    if (
        not isinstance(call_main, ast.Expr)
        or not isinstance(call_main.value, ast.Call)
        or not isinstance(call_main.value.func, ast.Name)
        or call_main.value.func.id != "main"
        or call_main.value.args
        or call_main.value.keywords
        or guard_node.orelse
    ):
        raise OracleError("oracle main guard must call main()")

    assertion = function_node.body[0]
    test = assertion.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        raise OracleError("oracle assertion must be one equality or None check")
    operation = _call(test.left, "assertion")
    operator = test.ops[0]
    expected = _literal(test.comparators[0], "expected value")
    if isinstance(operator, ast.Eq):
        comparison = "eq"
    elif isinstance(operator, ast.Is) and expected is None:
        comparison = "is_none"
    else:
        raise OracleError("oracle assertion must use == or is None")
    if assertion.msg is not None:
        message_operation = _call(assertion.msg, "assertion message")
        if not _same_operation(operation, message_operation):
            raise OracleError("oracle assertion message must repeat its operation")
    return OracleSpec(operation.function, operation.args, operation.kwargs, comparison, expected)


def _workspace_is_safe(workspace: Path, oracle_path: Path) -> None:
    workspace = Path(workspace).resolve()
    oracle_path = Path(oracle_path).resolve()
    if not workspace.is_dir():
        raise OracleError("candidate workspace must be an existing directory")
    try:
        oracle_path.relative_to(workspace)
    except ValueError:
        return
    raise OracleError("oracle must not be inside the candidate workspace")


def _candidate_command(operation: OracleOperation, workspace: Path, image: str, container_name: str) -> list[str]:
    """Build the Docker command; ``operation`` contains no expected value."""

    args = _json(list(operation.args), "operation arguments")
    kwargs = _json(dict(operation.kwargs), "operation keywords")
    return [
        "docker",
        "run",
        "--name",
        container_name,
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        "512m",
        "--cpus",
        "1",
        "--pids-limit",
        "64",
        "--workdir",
        "/work",
        "--mount",
        f"type=bind,source={Path(workspace).resolve()},target=/work,readonly",
        image,
        "python",
        "-I",
        "-B",
        "-c",
        _RUNNER_SOURCE,
        operation.function,
        args,
        kwargs,
    ]


def _runner_payload(stdout: str) -> Optional[dict[str, Any]]:
    marker_at = stdout.rfind(_RESULT_MARKER)
    if marker_at < 0:
        return None
    line = stdout[marker_at + len(_RESULT_MARKER):].splitlines()[0]
    try:
        payload = json.loads(line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _capture_bounded(pipe: Any, target: list[bytes]) -> None:
    """Drain one child pipe while retaining only its bounded tail."""
    tail = bytearray()
    try:
        while True:
            chunk = pipe.read(8192)
            if not chunk:
                break
            tail.extend(chunk)
            if len(tail) > OUTPUT_LIMIT:
                del tail[:-OUTPUT_LIMIT]
    finally:
        try:
            pipe.close()
        except OSError:
            pass
        target.append(bytes(tail))


def _run_bounded(command: list[str]) -> tuple[Optional[int], bool, str, str]:
    """Run the candidate with continuously drained, bounded stdout/stderr."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout_tail: list[bytes] = []
    stderr_tail: list[bytes] = []
    readers = [
        threading.Thread(
            target=_capture_bounded, args=(process.stdout, stdout_tail), daemon=True,
        ),
        threading.Thread(
            target=_capture_bounded, args=(process.stderr, stderr_tail), daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        returncode = process.wait(timeout=ORACLE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        returncode = process.wait()
    finally:
        for reader in readers:
            reader.join(timeout=5)

    stdout = (stdout_tail[0] if stdout_tail else b"").decode("utf-8", "replace")
    stderr = (stderr_tail[0] if stderr_tail else b"").decode("utf-8", "replace")
    return returncode, timed_out, stdout, stderr


def _values_equal(actual: Any, expected: Any) -> bool:
    """Compare JSON values without Python's bool-is-int equality surprise."""

    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return (
            set(actual.keys()) == set(expected.keys())
            and all(_values_equal(actual[key], expected[key]) for key in actual)
        )
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(
            _values_equal(left, right) for left, right in zip(actual, expected)
        )
    return actual == expected


def _payload_matches(payload: Optional[dict[str, Any]], spec: OracleSpec) -> bool:
    if not isinstance(payload, dict) or payload.get("ok") is not True or "value" not in payload:
        return False
    return (
        payload["value"] is None
        if spec.operator == "is_none"
        else _values_equal(payload["value"], spec.expected)
    )


def docker_oracle(scenario: Any, workspace: Path, image: str) -> dict[str, Any]:
    """Evaluate a candidate operation without mounting or executing the oracle.

    The expected value is parsed and retained only in this trusted host
    process.  The Docker command receives the service method name and literal
    arguments from the left side of the assertion; it never receives the
    expected value or the oracle path.
    """

    oracle_path = Path(scenario.oracle_path)
    oracle_sha256 = getattr(scenario, "oracle_sha256", None)
    if not isinstance(oracle_sha256, str):
        raise OracleError("scenario must provide the immutable oracle digest")
    spec = parse_oracle(oracle_path, oracle_sha256)
    _workspace_is_safe(Path(workspace), oracle_path)
    container_name = f"engraphis-benchmark-{uuid.uuid4().hex}"
    command = _candidate_command(spec.operation, Path(workspace), image, container_name)
    try:
        returncode, timed_out, stdout, stderr = _run_bounded(command)
        if timed_out:
            return {"passed": False, "returncode": None, "timed_out": True,
                    "oracle_outcome": "timeout_unknown", "stdout": "", "stderr": "oracle timeout"}
        payload = _runner_payload(stdout)
        if (
            isinstance(payload, dict)
            and payload.get("ok") is False
            and isinstance(payload.get("error_type"), str)
            and payload["error_type"].strip()
        ):
            # The runner emits this stable marker before re-raising, so a
            # candidate exception is scoreable even though Python exits nonzero.
            oracle_outcome = "candidate_exception"
        elif returncode != 0:
            # A non-zero container status does not distinguish candidate
            # failure from Docker/runtime failure. Keep it unscored so a
            # correction cannot be driven by infrastructure diagnostics.
            oracle_outcome = "ambiguous_nonzero"
        elif not isinstance(payload, dict) or payload.get("ok") is not True or "value" not in payload:
            oracle_outcome = "ambiguous_zero_exit"
        else:
            # A valid zero-exit payload with the wrong value is a real task
            # failure and remains eligible for the bounded correction loop.
            oracle_outcome = "passed" if _payload_matches(payload, spec) else "value_mismatch"
        passed = oracle_outcome == "passed"
        return {
            "passed": passed,
            "returncode": returncode,
            "timed_out": False,
            "oracle_outcome": oracle_outcome,
            "stdout": stdout,
            "stderr": stderr,
        }
    finally:
        # Only the container name created by this invocation is addressed.
        try:
            subprocess.run(
                ["docker", "rm", "--force", container_name],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass


__all__ = [
    "ALLOWED_FUNCTIONS",
    "OracleError",
    "OracleOperation",
    "OracleSpec",
    "docker_oracle",
    "parse_oracle",
]
