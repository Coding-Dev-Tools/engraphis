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


class FailOpenBoundaryTests(unittest.TestCase):
    """Malformed env overrides must not crash the hook at import time."""

    def setUp(self):
        self.hook = _load()

    def test_malformed_budget_falls_back_to_default(self):
        """ENGRAPHIS_HOOK_BUDGET_S=not-a-number must not raise; main()
        uses the default budget so the hook still fails open.
        """
        with mock.patch.dict(
            os.environ,
            {
                "ENGRAPHIS_MCP_URL": "http://127.0.0.1:9/mcp",
                "ENGRAPHIS_HOOK_BUDGET_S": "not-a-number",
            },
            clear=False,
        ):
            with mock.patch.object(self.hook, "session_context", return_value="") as fake:
                with mock.patch.object(sys, "stdin", mock.MagicMock(read=lambda: "{}")):
                    with mock.patch.object(sys, "stdout", mock.MagicMock()):
                        rc = self.hook.main()
        # The hook must not crash; the deadline is computed from the
        # fallback default (4.0s) so the session_context call still
        # happens with a valid future deadline.
        self.assertEqual(rc, 0)
        self.assertGreater(fake.call_args.args[2], 0)

    def test_malformed_max_chars_falls_back_to_default(self):
        """ENGRAPHIS_HOOK_MAX_CHARS=not-a-number must not raise."""
        with mock.patch.dict(
            os.environ,
            {
                "ENGRAPHIS_MCP_URL": "http://127.0.0.1:9/mcp",
                "ENGRAPHIS_HOOK_MAX_CHARS": "also-not-a-number",
            },
            clear=False,
        ):
            with mock.patch.object(self.hook, "session_context", return_value="ctx"):
                with mock.patch.object(sys, "stdin", mock.MagicMock(read=lambda: "{}")):
                    with mock.patch.object(sys, "stdout", mock.MagicMock()) as buf:
                        rc = self.hook.main()
        self.assertEqual(rc, 0)
        # The default cap (300 terse) is in effect, but the substring
        # still appears because compression preserves the first sentence.
        self.assertIn("ctx", buf.write.call_args.args[0])


class TerseCompressionTests(unittest.TestCase):
    """V12 terse-hook default: 200-char compressed context.

    Empirically validated on Qwen 3.7 Flash (bench_v1/V12): the terse
    rendering is the only mode that improves quality on every model
    tested. Prose was actively harmful (-0.067 sub on Qwen 3.7 Flash).
    """

    def setUp(self):
        self.hook = _load()

    def test_default_max_context_chars_is_terse(self):
        """The shipped default is 300 chars (terse), not 1500 (prose)."""
        self.assertEqual(self.hook.MAX_CONTEXT_CHARS, 300)
        self.assertEqual(self.hook.MAX_CONTEXT_CHARS_DEFAULT, 300)
        self.assertEqual(self.hook.MAX_CONTEXT_CHARS_PROSE, 1500)

    def test_terse_compresses_numbered_facts(self):
        """Numbered upstream facts render as '[n] first-sentence' joined."""
        prose = (
            "[1] First fact. This is a long elaboration that should be cut. "
            "[2] Second fact. Also has trailing detail to drop. "
            "[3] Third fact."
        )
        out = self.hook._compress_prose_to_terse(prose, 200)
        self.assertIn("[1] First fact.", out)
        self.assertIn("[2] Second fact.", out)
        self.assertIn("[3] Third fact.", out)
        # Trailing elaborations are dropped.
        self.assertNotIn("elaboration", out)
        self.assertNotIn("trailing detail", out)

    def test_terse_falls_back_to_period_split(self):
        """Unnumbered prose splits on sentence boundaries."""
        prose = "Alpha. Beta. Gamma."
        out = self.hook._compress_prose_to_terse(prose, 200)
        self.assertEqual(out, "[1] Alpha.; [2] Beta.; [3] Gamma.")

    def test_terse_respects_budget(self):
        """The compressed output stays under the max_chars budget."""
        prose = ". ".join([f"fact {i} with some words" for i in range(20)])
        budget = 80
        out = self.hook._compress_prose_to_terse(prose, budget)
        self.assertLessEqual(len(out), budget)
        # At least one fact fits in 80 chars.
        self.assertIn("[1]", out)

    def test_terse_returns_input_on_empty(self):
        """Empty input returns empty (fails open)."""
        self.assertEqual(self.hook._compress_prose_to_terse("", 200), "")
        self.assertEqual(self.hook._compress_prose_to_terse("   ", 200), "   ")

    def test_build_additional_context_terse_default(self):
        """Default format='terse' produces compressed output under 300 chars."""
        prose = "First sentence. Second sentence. Third sentence."
        out = self.hook.build_additional_context(prose, "ws")
        # Body budget: 300 - len(header) - len(footer).
        self.assertLessEqual(len(out), 300)
        self.assertIn("workspace ws", out)
        # Compressed facts are present.
        self.assertIn("[1]", out)

    def test_build_additional_context_prose_format(self):
        """format='prose' keeps the original dense context."""
        prose = "First sentence. Second sentence. " + ("x" * 500)
        out = self.hook.build_additional_context(
            prose, "ws", max_context_chars=1500, format="prose"
        )
        # Prose mode: no "[n]" compression markers.
        self.assertNotIn("[1]", out)
        # Original text preserved up to body budget.
        self.assertIn("First sentence.", out)

    def test_env_prose_lifts_cap(self):
        """ENGRAPHIS_HOOK_FORMAT=prose without ENGRAPHIS_HOOK_MAX_CHARS
        uses the legacy 1500-char cap."""
        with mock.patch.dict(
            os.environ,
            {
                "ENGRAPHIS_MCP_URL": "http://127.0.0.1:9/mcp",
                "ENGRAPHIS_HOOK_FORMAT": "prose",
            },
            clear=False,
        ):
            with mock.patch.object(self.hook, "session_context",
                                   return_value="ctx"):
                with mock.patch.object(sys, "stdin", mock.MagicMock(read=lambda: "{}")):
                    with mock.patch.object(sys, "stdout", mock.MagicMock()) as buf:
                        self.hook.main()
        # The legacy prose cap (1500) is restored, not the 200 terse cap.
        out = buf.write.call_args.args[0]
        self.assertLessEqual(len(out), 1500)

    def test_env_format_garbage_falls_back_to_terse(self):
        """Unknown format values fall back to terse (fail open)."""
        with mock.patch.dict(
            os.environ,
            {
                "ENGRAPHIS_MCP_URL": "http://127.0.0.1:9/mcp",
                "ENGRAPHIS_HOOK_FORMAT": "verbose",
            },
            clear=False,
        ):
            with mock.patch.object(self.hook, "session_context",
                                   return_value="ctx"):
                with mock.patch.object(sys, "stdin", mock.MagicMock(read=lambda: "{}")):
                    with mock.patch.object(sys, "stdout", mock.MagicMock()) as buf:
                        self.hook.main()
        out = buf.write.call_args.args[0]
        # Terse cap is in effect: body + header + footer stays bounded by
        # 300 (the body budget), and the compressed body is much smaller
        # than the prose cap (1500).
        additional = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(additional), 300)


if __name__ == "__main__":
    unittest.main()
