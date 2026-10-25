"""Desktop memory regressions: real persistence, routing and exact lookup."""
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import io
import json
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest

from engraphis.core.recall import _exact_identifier_pattern
from engraphis.service import MemoryService


ROOT = Path(__file__).resolve().parents[1]


def _standalone(relative):
    spec = importlib.util.spec_from_file_location("desktop_test_module", ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_memory_survives_origin_session_and_three_client_restarts(tmp_path):
    path = str(tmp_path / "shared.db")
    original = MemoryService.create(path)
    try:
        session = original.start_session("desktop-test", repo="shared", agent="command-code")
        saved = original.remember(
            "The shared desktop verification phrase is copper lantern.",
            session_id=session["session_id"], trusted=True,
            _local_agent_operator=True, _ingress="mcp",
        )
        assert saved["scope"] == "repo"
        assert saved["prompt_eligible"] is True
        assert saved["review_state"] == "approved"
        original.end_session(session["session_id"], summary="Saved desktop test phrase.")
    finally:
        original.close()
    for agent in ("chatgpt", "claude-desktop", "command-code"):
        reader = MemoryService.create(path)
        try:
            session = reader.start_session("desktop-test", repo="shared", agent=agent)
            recalled = reader.recall("shared desktop copper lantern", session_id=session["session_id"], k=3)
            assert saved["id"] in {memory["id"] for memory in recalled["memories"]}
        finally:
            reader.close()


def test_remember_reports_pending_external_content_and_noop_target():
    service = MemoryService.create(":memory:")
    try:
        options = {"workspace": "test", "source": "web", "trusted": True,
                   "_local_agent_operator": True, "_ingress": "mcp"}
        saved = service.remember("The external catalog reports a copper lantern.", **options)
        assert saved["stored"] is True
        assert saved["prompt_eligible"] is False
        assert saved["review_state"] == "pending"
        repeated = service.remember("The external catalog reports a copper lantern.", **options)
        assert repeated["prompt_eligible"] is False
        assert repeated["review_state"] == "pending"
        local = {"workspace": "test", "_local_agent_operator": True, "_ingress": "mcp"}
        approved = service.remember("The local desktop phrase is amber compass.", **local)
        noop = service.remember("The local desktop phrase is amber compass.", **local)
        assert noop["op"] == "noop" and noop["id"] == approved["id"]
        assert noop["prompt_eligible"] is True and noop["review_state"] == "approved"
    finally:
        service.close()


@pytest.mark.parametrize("identifier", ["20261020-ENGRAPHIS-7QF", "desktop_memory_7QF",
                                        "desktop/memory/7QF", "desktop.memory.7QF"])
@pytest.mark.parametrize("fts", [True, False])
def test_exact_identifier_outweighs_unrelated_lifecycle_priors(identifier, fts):
    service = MemoryService.create(":memory:")
    try:
        service.store.has_fts5 = fts and service.store.has_fts5
        options = {"workspace": "test", "repo": "shared", "resolve_conflicts": False,
                   "_local_agent_operator": True, "_ingress": "mcp"}
        marker = service.remember(f"Desktop diagnostic marker {identifier}: copper lantern.",
                                  mtype="working", importance=0.2, **options)
        for index in range(6):
            service.remember(f"Engraphis desktop memory policy number {index} covers release procedures.",
                             mtype="semantic", importance=0.9, **options)
        recalled = service.recall(identifier.lower(), workspace="test", repo="shared", k=3,
                                  token_budget=400)
        assert recalled["memories"][0]["id"] == marker["id"]
        assert recalled["packed_sources"][0]["id"] == marker["id"]
    finally:
        service.close()


def test_exact_identifier_does_not_bypass_scope_review_or_validity():
    service = MemoryService.create(":memory:")
    try:
        options = {"workspace": "test", "repo": "shared", "resolve_conflicts": False,
                   "_local_agent_operator": True, "_ingress": "mcp"}
        pending = service.remember("Marker desktop-memory-7QF from external notes.",
                                   **{**options, "source": "web", "trusted": True})
        other = service.remember("Marker desktop-memory-7QF for another project.",
                                 **{**options, "repo": "private"})
        closed = service.remember("Marker desktop-memory-7QF was retired.", **options)
        service.store.close_validity(closed["id"])
        result = service.recall("desktop-memory-7QF", workspace="test", repo="shared", k=10)
        assert {pending["id"], other["id"], closed["id"]}.isdisjoint(
            memory["id"] for memory in result["memories"]
        )
    finally:
        service.close()


def test_literal_priority_uses_whole_content_tokens_only():
    pattern = _exact_identifier_pattern("desktop-memory-7qf")
    assert pattern.search("marker DESKTOP-MEMORY-7QF.".casefold())
    assert not pattern.search("prefix-desktop-memory-7qf-extra")
    assert not pattern.search("desktop-memory-7qf_extra")
    assert _exact_identifier_pattern("what is desktop memory") is None


def test_commandcode_hook_exposes_session_even_without_recalled_content(monkeypatch):
    hook = _standalone("integrations/commandcode/session_start_hook.py")
    sid = "ses_01M5BRSZ3F0XNJKXPC2QKN3YG2"
    result = {"content": [{"type": "text", "text": json.dumps({
        "context": "", "session_id": sid, "workspace": "client-acme", "repo": "shared",
    })}]}
    calls = []

    def rpc(method, params, *_args, **_kwargs):
        calls.append((method, params))
        return (result if method == "tools/call" else {}), "http-session"

    monkeypatch.setattr(hook, "rpc", rpc)
    monkeypatch.setattr(hook, "notify_initialized", lambda *_args, **_kwargs: "http-session")
    monkeypatch.setattr(hook.sys, "stdin", io.StringIO('{"cwd":"C:/test/shared"}'))
    output = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", output)
    assert hook.main() == 0
    additional = json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
    assert sid in additional and "client-acme" in additional
    assert calls[-1][1]["arguments"]["agent"] == "command-code"


def test_terse_hook_preserves_routing_before_numbered_memory_context():
    hook = _standalone("integrations/commandcode/session_start_hook.py")
    sid = "ses_01M5BRSZ3F0XNJKXPC2QKN3YG2"
    result = {"content": [{"type": "text", "text": json.dumps({
        "context": "[1] The project uses pnpm. [2] The runner uses pytest.",
        "session_id": sid, "workspace": "acme", "repo": "shared",
    })}]}
    context, workspace = hook.extract_session_result(result)
    output = hook.build_additional_context(context, workspace, 300, format="terse")
    assert sid in output and '"workspace": "acme"' in output
    assert "[1]" in output
    assert len(output) <= 300


@pytest.mark.parametrize("url", ["https://127.0.0.1/mcp", "http://example.com/mcp",
                                 "http://127.0.0.1:8711/mcp?token=secret",
                                 "http://user:pass@127.0.0.1:8711/mcp"])
def test_bridge_rejects_remote_or_credentialed_urls(url):
    bridge = _standalone("engraphis/mcp_stdio_bridge.py")
    with pytest.raises(argparse.ArgumentTypeError):
        bridge.loopback_url(url)


def test_bridge_decodes_sse_results_and_errors():
    bridge = _standalone("engraphis/mcp_stdio_bridge.py")
    result = {"jsonrpc": "2.0", "id": 7, "error": {"code": -32601, "message": "unknown tool"}}
    body = ("event: message\r\ndata: " + json.dumps(result) + "\r\n\r\n").encode()
    assert bridge.response_message(body, 7) == result
    with pytest.raises(ValueError):
        bridge.response_message(body, 8)


def test_bridge_forwards_negotiated_session_and_protocol_headers():
    module = _standalone("engraphis/mcp_stdio_bridge.py")
    bridge = module.LocalBridge("http://127.0.0.1:8711/mcp")
    requests = []

    class Response:
        headers = {"Mcp-Session-Id": "transport-session"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _limit):
            request = requests[-1]
            message = json.loads(request.data)
            if "id" not in message:
                return b""
            return json.dumps({"jsonrpc": "2.0", "id": message["id"],
                               "result": {"protocolVersion": "2025-03-26"}}).encode()

    def open_request(request, **_kwargs):
        requests.append(request)
        return Response()

    bridge.opener = SimpleNamespace(open=open_request)
    bridge.forward({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert bridge.forward({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    bridge.forward({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    headers = {key.lower(): value for key, value in requests[-1].header_items()}
    assert headers["mcp-session-id"] == "transport-session"
    assert headers["mcp-protocol-version"] == "2025-03-26"


@pytest.mark.parametrize("terminal", ["result", "error"])
def test_bridge_returns_completed_sse_result_before_stream_closes(terminal, monkeypatch):
    from engraphis.mcp_stdio_bridge import LocalBridge

    # The offline suite replaces every DNS result with a public fixture address.
    # This protocol test connects only to its own numeric loopback server.
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *_a, **_k: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", port)),
    ])
    release = threading.Event()
    expected = {"jsonrpc": "2.0", "id": 7, terminal: {"value": "copper lantern"}}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.end_headers()
            self.wfile.write(b": keepalive\n\nevent: message\n")
            self.wfile.write(("data: " + json.dumps(expected) + "\n\n").encode())
            self.wfile.flush()
            release.wait(3)
            self.close_connection = True

        def log_message(self, *_args):
            pass

    with HTTPServer(("127.0.0.1", 0), Handler) as http:
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        try:
            bridge = LocalBridge(f"http://127.0.0.1:{http.server_port}/mcp", timeout=0.5)
            assert bridge.forward({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}) == expected
            assert not release.is_set()
        finally:
            release.set()
            http.shutdown()
            worker.join(timeout=3)


def test_sse_stream_limit_includes_ignored_events(monkeypatch):
    from engraphis import mcp_stdio_bridge as bridge

    monkeypatch.setattr(bridge, "MAX_MESSAGE_BYTES", 40)
    response = io.BytesIO(b": " + b"x" * 60 + b"\n\n")
    response.headers = {"Content-Type": "text/event-stream"}
    with pytest.raises(ValueError, match="message limit"):
        bridge.read_response(response, 7)
