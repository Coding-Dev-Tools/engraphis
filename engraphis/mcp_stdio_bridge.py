"""Dependency-free stdio bridge to an already running local Engraphis MCP service.

Run this file directly with Python, including ``python -I /path/to/this/file.py``.
Desktop clients can share one warmed engine instead of each loading PyTorch.
Only numeric loopback HTTP endpoints are accepted; proxies and redirects are disabled.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import math
import sys
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


MAX_MESSAGE_BYTES = 4_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use a numeric loopback HTTP MCP URL") from exc
    if (
        parsed.scheme != "http" or not address.is_loopback
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or port == 0
    ):
        raise argparse.ArgumentTypeError("use a numeric loopback HTTP MCP URL without credentials")
    return value


def response_message(body: bytes, request_id):
    """Decode JSON or bounded Streamable HTTP SSE, preserving protocol errors."""
    text = body.decode("utf-8")
    try:
        messages = [json.loads(text)] if text.strip() else []
    except ValueError:
        messages = []
        data = []
        for line in [*text.splitlines(), ""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif not line and data:
                messages.append(json.loads("\n".join(data)))
                data = []
    for message in messages:
        if (
            isinstance(message, dict) and message.get("jsonrpc") == "2.0"
            and message.get("id") == request_id
            and ("result" in message or "error" in message)
        ):
            return message
    raise ValueError("local MCP response did not contain the requested result")


def read_response(response, request_id):
    """Finish at the matching SSE result, without waiting for a stream to close."""
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type != "text/event-stream":
        body = response.read(MAX_MESSAGE_BYTES + 1)
        if len(body) > MAX_MESSAGE_BYTES:
            raise ValueError("local MCP response exceeded the message limit")
        return response_message(body, request_id)

    size = 0
    data = []
    while True:
        line = response.readline(MAX_MESSAGE_BYTES - size + 1)
        size += len(line)
        if size > MAX_MESSAGE_BYTES:
            raise ValueError("local MCP response exceeded the message limit")
        if not line:
            break
        text = line.decode("utf-8").rstrip("\r\n")
        if text.startswith("data:"):
            data.append(text[5:].lstrip(" "))
        elif not text and data:
            message = json.loads("\n".join(data))
            data = []
            if (isinstance(message, dict) and message.get("jsonrpc") == "2.0"
                    and message.get("id") == request_id
                    and ("result" in message or "error" in message)):
                return message
    raise ValueError("local MCP response did not contain the requested result")


class LocalBridge:
    def __init__(self, url: str, timeout: float = 120.0):
        self.url = loopback_url(url)
        self.timeout = timeout
        self.session_id = None
        self.protocol_version = None
        self.opener = build_opener(ProxyHandler({}), _NoRedirect())

    def forward(self, message: dict):
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        request = Request(self.url, data=json.dumps(message).encode("utf-8"),
                          headers=headers, method="POST")
        with self.opener.open(request, timeout=self.timeout) as response:
            self.session_id = response.headers.get("Mcp-Session-Id") or self.session_id
            if "id" not in message:
                return None
            result = read_response(response, message["id"])
        if message.get("method") == "initialize" and "result" in result:
            self.protocol_version = result["result"].get("protocolVersion")
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", type=loopback_url, default="http://127.0.0.1:8711/mcp")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or not 0 < args.timeout <= 300:
        parser.error("timeout must be finite and between 0 and 300 seconds")
    bridge = LocalBridge(args.url, args.timeout)
    while True:
        line = sys.stdin.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return 0
        message = None
        try:
            if len(line.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise ValueError("request exceeds the message limit")
            message = json.loads(line)
            if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                raise ValueError("invalid JSON-RPC envelope")
            result = bridge.forward(message)
        except Exception as exc:
            # Never echo request content, URLs or backend exception strings.
            print("Engraphis local MCP bridge failed: " + type(exc).__name__, file=sys.stderr)
            if isinstance(message, dict) and "id" not in message:
                continue
            result = {"jsonrpc": "2.0", "id": message.get("id") if isinstance(message, dict) else None,
                      "error": {"code": -32000, "message": "Local Engraphis MCP service unavailable"}}
        if result is not None:
            sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
