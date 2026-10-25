"""Fail-closed Command Code PreToolUse hook for operator-protected files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Dict


# A PreToolUse payload is a small JSON object.  Bounding its size and nesting
# keeps a hostile or broken payload from exhausting memory or the JSON
# parser's stack, which would otherwise turn a denial into a crash.
MAX_PAYLOAD_BYTES = 1 << 20
MAX_PAYLOAD_DEPTH = 64


def _report(message: str) -> None:
    """Print a denial reason without ever raising.

    The host only blocks a tool call when this process exits with status 2, so
    a failure while reporting the denial must not change the exit status.
    """
    try:
        print(message, file=sys.stderr)
    except Exception:  # pragma: no cover - defensive: stderr may be closed
        try:
            sys.stderr.flush()
        except Exception:
            pass


def _max_json_depth(text: str) -> int:
    """Return the deepest bracket nesting in ``text``.

    The scan is iterative, so a hostile payload cannot exhaust the stack while
    it is being inspected.
    """
    depth = 0
    deepest = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > deepest:
                deepest = depth
        elif char in "]}":
            depth -= 1
    return deepest


def _read_payload() -> Dict[str, Any]:
    """Read and validate the host's JSON payload."""
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    try:
        raw = stream.read(MAX_PAYLOAD_BYTES + 1)
    except Exception as exc:
        raise ValueError(f"cannot read the hook payload: {exc}") from exc
    if isinstance(raw, str):
        data = raw.encode("utf-8", "surrogatepass")
    else:
        data = raw
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError("the hook payload is larger than the accepted size")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"the hook payload is not valid UTF-8: {exc}") from exc
    if _max_json_depth(text) > MAX_PAYLOAD_DEPTH:
        raise ValueError("the hook payload is nested too deeply")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("the hook payload must be a JSON object")
    return payload


def main() -> int:
    # Command Code lets the tool proceed on any exit status other than 2, so
    # every failure -- including a missing ``engraphis`` install, deep JSON
    # recursion, or an unexpected bug -- must be reported as a denial.
    try:
        from engraphis.file_guard import decide_tool_call, load_policy

        payload = _read_payload()
        raw_tool_input = payload.get("tool_input")
        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        decision = decide_tool_call(
            str(payload.get("tool_name") or ""),
            tool_input,
            cwd=Path(str(payload.get("cwd") or os.getcwd())),
            rules=load_policy(),
        )
    except BaseException as exc:  # fail closed on *any* error, including bugs
        # A configured security hook must never fail open.
        _report(f"Engraphis file guard denied the tool call: {exc!r}")
        return 2
    if decision.allowed:
        return 0
    detail = f" (matched {decision.matched_pattern})" if decision.matched_pattern else ""
    _report(f"Engraphis file guard denied the tool call: {decision.reason}{detail}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
