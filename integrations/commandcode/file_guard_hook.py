"""Fail-closed Command Code PreToolUse hook for operator-protected files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys



def main() -> int:
    # Command Code lets the tool proceed on any exit status other than 2, so
    # every failure -- including a missing ``engraphis`` install, deep JSON
    # recursion, or an unexpected bug -- must be reported as a denial.
    try:
        from engraphis.file_guard import decide_tool_call, load_policy

        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise ValueError("hook payload must be an object")
        raw_tool_input = payload.get("tool_input")
        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        decision = decide_tool_call(
            str(payload.get("tool_name") or ""),
            tool_input,
            cwd=Path(str(payload.get("cwd") or os.getcwd())),
            rules=load_policy(),
        )
    except Exception as exc:
        # A configured security hook must never fail open.
        print(f"Engraphis file guard denied the tool call: {exc}", file=sys.stderr)
        return 2
    if decision.allowed:
        return 0
    detail = f" (matched {decision.matched_pattern})" if decision.matched_pattern else ""
    print(f"Engraphis file guard denied the tool call: {decision.reason}{detail}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
