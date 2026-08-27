# -*- coding: utf-8 -*-
"""Unit tests for integrations.commandcode.session_start_hook.

These tests exercise the pure-function surface (resolve_workspace,
build_additional_context) plus the JSON-RPC error paths. They do NOT call the
real MCP server; live integration is covered by the rebench end-to-end proof.
"""
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.normpath(os.path.join(
    ROOT, "..", "integrations", "commandcode", "session_start_hook.py"))


def _load():
    spec = importlib.util.spec_from_file_location("session_start_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WorkspaceResolution(unittest.TestCase):
    def setUp(self):
        self.hook = _load()

    def test_default_falls_back_to_repo_basename(self):
        self.assertEqual(
            self.hook.resolve_workspace("C:/work/engraphis", {}),
            "engraphis",
        )

    def test_override_wins_over_basename(self):
        self.assertEqual(
            self.hook.resolve_workspace(
                "C:/work/engraphis", {"ENGRAPHIS_HOOK_WORKSPACE": "ops-prod"}),
            "ops-prod",
        )

    def test_blank_override_falls_back(self):
        self.assertEqual(
            self.hook.resolve_workspace(
                "C:/work/engraphis", {"ENGRAPHIS_HOOK_WORKSPACE": "   "}),
            "engraphis",
        )


class ContextBuild(unittest.TestCase):
    def setUp(self):
        self.hook = _load()

    def test_header_carries_workspace_name(self):
        out = self.hook.build_additional_context("hello", "ops-prod")
        self.assertIn("workspace ops-prod", out)
        self.assertIn("hello", out)
        self.assertIn("mcp__engraphis__", out)

    def test_truncates_at_budget(self):
        original = self.hook.MAX_CONTEXT_CHARS
        self.hook.MAX_CONTEXT_CHARS = 50
        try:
            out = self.hook.build_additional_context("x" * 10_000, "ws")
        finally:
            self.hook.MAX_CONTEXT_CHARS = original
        # Header + footer + truncated body, never exceeds the budget.
        self.assertLessEqual(len(out), 50)


class EndToEndBehavior(unittest.TestCase):
    def setUp(self):
        self.hook = _load()

    def test_fails_open_on_unreachable_server(self):
        with mock.patch.object(self.hook, "MCP_URL", "http://127.0.0.1:9/mcp"):
            with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({
                "hook_event_name": "SessionStart",
                "cwd": "C:/work/engraphis",
            }))):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    self.assertEqual(self.hook.main(), 0)
        self.assertEqual(buf.getvalue(), "")

    def test_wrong_event_prints_nothing(self):
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({
            "hook_event_name": "PreToolUse",
            "cwd": "C:/work/engraphis",
        }))):
            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                self.assertEqual(self.hook.main(), 0)
        self.assertEqual(buf.getvalue(), "")

    def test_workspace_override_used_in_call(self):
        with mock.patch.object(self.hook, "MCP_URL", "http://127.0.0.1:9/mcp"):
            with mock.patch.object(self.hook, "session_context",
                                   return_value="") as fake:
                with mock.patch.dict(os.environ,
                                     {"ENGRAPHIS_HOOK_WORKSPACE": "ops"}):
                    with mock.patch.object(sys, "stdin", io.StringIO(json.dumps({
                        "hook_event_name": "SessionStart",
                        "cwd": "C:/work/engraphis",
                    }))):
                        buf = io.StringIO()
                        with mock.patch.object(sys, "stdout", buf):
                            self.hook.main()
        self.assertEqual(fake.call_args.args[1], "ops")


if __name__ == "__main__":
    unittest.main()
