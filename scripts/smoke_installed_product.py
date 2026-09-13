"""Exercise installed MCP/server journeys using disposable, offline local state.

Unlike the wrapper ``--help`` smoke, this starts real processes and verifies writes,
restart persistence, correction history and current/historical recall. It never
uses the operator's config, database, credentials, model cache or remote services.
Run from outside the checkout after installing a wheel with [mcp] or [server].
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


WORKSPACE = "installed-smoke"
ORIGINAL = "The Atlas deployment target is staging."
CORRECTED = "The Atlas deployment target is production."
_RUNTIME_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL"}


def isolated_environment(root: Path, *, installed: bool = True) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key.upper() in _RUNTIME_ENV}
    config = root / "config.env"
    config.write_text("", encoding="utf-8")
    config.chmod(0o600)
    env.update({
        "HOME": str(root), "USERPROFILE": str(root),
        "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
        "TMP": str(root), "TEMP": str(root), "TMPDIR": str(root),
        "ENGRAPHIS_ENV_FILE": str(config), "ENGRAPHIS_STATE_DIR": str(root / "state"),
        "ENGRAPHIS_DB_PATH": str(root / "memory.db"),
        "ENGRAPHIS_EMBED_MODEL": "", "ENGRAPHIS_RERANK_MODEL": "",
        "ENGRAPHIS_EXTRACTOR": "none", "ENGRAPHIS_GRAPH_EXTRACTOR": "none",
        "ENGRAPHIS_VECTOR_BACKEND": "numpy", "ENGRAPHIS_SERVICE_MODE": "customer",
        "ENGRAPHIS_UPDATE_CHECK": "0", "ENGRAPHIS_LLM_AUTO_EXTRACT": "0",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "PYTHONNOUSERSITE": "1", "PYTHONUNBUFFERED": "1",
    })
    if not installed:  # Source regression tests only; CLI always requires an installed artifact.
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return env


def _command(name: str, *, installed: bool) -> list[str]:
    if installed:
        from scripts.smoke_entry_points import console_script_path
        wrapper = console_script_path(name)
        if not wrapper.is_file():
            raise RuntimeError(f"missing installed wrapper: {name}")
        return [str(wrapper)]
    module = "engraphis.mcp_cli" if name == "engraphis-mcp" else "scripts.start_dashboard"
    return [sys.executable, "-m", module]


def _stop(process, *, clean: bool = False) -> None:
    from scripts.update import _kill_process_tree

    if process.stdin:
        process.stdin.close()
    try:
        process.wait(timeout=5 if clean else 0.1)
    except subprocess.TimeoutExpired:
        # Windows' generated console .exe launches a Python child. Kill the
        # complete owned process tree before the wrapper dies and loses lineage.
        _kill_process_tree(process)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if clean and process.returncode != 0:
        raise RuntimeError(f"MCP exited unsuccessfully: {process.returncode}")


class _Mcp:
    def __init__(self, command, env, root, timeout):
        self.command, self.env, self.root, self.timeout = command, env, root, timeout
        self.messages = queue.Queue()
        self.request_id = 0

    def __enter__(self):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            self.command, cwd=self.root, env=self.env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.errors, text=True, encoding="utf-8",
            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            initialized = self.request("initialize", {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "installed-product-smoke", "version": "1"},
            })
            assert initialized["serverInfo"]["name"] == "engraphis_mcp"
            self.notify("notifications/initialized", {})
            catalog = self.request("tools/list", {})
            names = {tool["name"] for tool in catalog["tools"]}
            assert {"engraphis_remember", "engraphis_recall_context", "engraphis_get_memory",
                    "engraphis_discover_actions", "engraphis_execute_action"} <= names
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1_000_001)
                if not line:
                    self.messages.put(None)
                    return
                if len(line) > 1_000_000:
                    raise RuntimeError("MCP response exceeds the smoke's bounded output limit")
                message = json.loads(line)  # Any non-JSON stdout is a protocol failure.
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise RuntimeError("invalid MCP JSON-RPC envelope")
                self.messages.put(message)
        except Exception as exc:
            self.messages.put(exc)

    def notify(self, method, params):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method,
                                            "params": params}) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.request_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.request_id,
                                            "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"MCP {method} timed out") from None
            if message is None:
                raise RuntimeError(f"MCP closed stdout during {method}")
            if isinstance(message, Exception):
                raise RuntimeError("MCP stdout is not valid bounded JSON-RPC") from message
            if message.get("id") != self.request_id:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"MCP {method} timed out")
                continue
            if "error" in message:
                raise RuntimeError(f"MCP {method} returned a protocol error")
            return message["result"]

    def call(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {name} rejected the smoke request")
        text = next(block["text"] for block in result["content"] if block["type"] == "text")
        payload = json.loads(text)
        if payload.get("error"):
            raise RuntimeError(f"MCP tool {name} returned an application error")
        return payload

    def __exit__(self, exc_type, *_args):
        try:
            _stop(self.process, clean=exc_type is None)
            self.reader.join(timeout=2)
            if exc_type is None:
                while not self.messages.empty():
                    if isinstance(self.messages.get_nowait(), Exception):
                        raise RuntimeError("MCP emitted invalid stdout during shutdown")
        finally:
            self.process.stdout.close()
            self.errors.close()


def mcp_journey(root, env, *, timeout, installed):
    command = _command("engraphis-mcp", installed=installed)
    with _Mcp(command, env, root, timeout) as client:
        saved = client.call("engraphis_remember", {
            "content": ORIGINAL, "workspace": WORKSPACE, "dedupe": False,
        })
        original_id = saved["id"]
        assert saved["stored"]
        original = client.call("engraphis_get_memory", {
            "memory_id": original_id, "workspace": WORKSPACE,
        })
        historical_at = max(original["valid_from"], original["ingested_at"])
    with _Mcp(command, env, root, timeout) as client:
        query = {"workspace": WORKSPACE, "query": "Atlas deployment target", "token_budget": 512}
        recalled = client.call("engraphis_recall_context", query)
        assert ORIGINAL in recalled["context"] and recalled["sources"]
        discovery = client.call("engraphis_discover_actions", {
            "task": "correct", "intent": "write", "limit": 3,
        })
        action = next(item for item in discovery["actions"] if item["canonical_action"] == "correct")
        corrected = client.call("engraphis_execute_action", {
            "capability_id": action["capability_id"], "schema_digest": action["schema_digest"],
            "arguments": {"memory_id": original_id, "new_content": CORRECTED,
                          "workspace": WORKSPACE, "reason": "installed journey"},
        })["result"]
        corrected_id = corrected["id"]
        assert corrected["superseded"] == [original_id]
    with _Mcp(command, env, root, timeout) as client:
        current = client.call("engraphis_recall_context", query)
        assert CORRECTED in current["context"] and ORIGINAL not in current["context"]
        discovery = client.call("engraphis_discover_actions", {
            "task": "recall_context", "limit": 3,
        })
        action = next(item for item in discovery["actions"]
                      if item["canonical_action"] == "recall_context")
        gateway = ("engraphis_execute_read" if action["side_effect"] == "read"
                   else "engraphis_execute_action")
        past = client.call(gateway, {
            "capability_id": action["capability_id"], "schema_digest": action["schema_digest"],
            "arguments": {**query, "valid_at": historical_at, "known_at": historical_at},
        })["result"]
        assert ORIGINAL in past["context"] and CORRECTED not in past["context"]
        history = client.call("engraphis_get_memory", {
            "memory_id": corrected_id, "workspace": WORKSPACE,
        })
        assert {item["id"] for item in history["chain"]} >= {original_id, corrected_id}
        assert history["provenance"]["trusted"] is True
    return ["initialize", "tools/list", "remember", "restart recall", "correction",
            "restart current and historical recall", "governed history and provenance"]


@contextmanager
def _dashboard(root, env, *, timeout, installed):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    opener = build_opener(ProxyHandler({}))

    def request(path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(origin + path, data=data, headers={
            "Content-Type": "application/json", "Origin": origin,
        })
        with opener.open(req, timeout=timeout) as response:
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise RuntimeError("dashboard response exceeds smoke output limit")
            return json.loads(body) if "json" in response.headers.get("Content-Type", "") else body

    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            _command("engraphis-dashboard", installed=installed) + [
                "--no-open", "--host", "127.0.0.1", "--port", str(port),
            ], cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=os.name != "nt",
        )
        try:
            deadline = time.monotonic() + timeout
            while True:
                if process.poll() is not None:
                    raise RuntimeError("installed dashboard exited before readiness")
                try:
                    if request("/api/ready").get("ready"):
                        break
                except (URLError, TimeoutError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("installed dashboard readiness timed out")
                time.sleep(0.1)
            assert request("/api/health")
            build = request("/api/build")
            assert build["database_schema_version"] == build["schema_version"]
            assert len(build["package_source_sha256"]) == 64
            assert b"<html" in request("/").lower()
            yield request
        finally:
            _stop(process)


def server_journey(root, env, *, timeout, installed):
    query = "/api/recall?" + urlencode({"workspace": WORKSPACE, "q": "Atlas deployment target"})
    with _dashboard(root, env, timeout=timeout, installed=installed) as request:
        saved = request("/api/remember", {"workspace": WORKSPACE, "content": ORIGINAL,
                                           "dedupe": False})
        original_id = saved["id"]
        original = request(f"/api/memory/{original_id}?workspace={WORKSPACE}")["memory"]
        historical_at = max(original["valid_from"], original["ingested_at"])
    with _dashboard(root, env, timeout=timeout, installed=installed) as request:
        assert ORIGINAL in request(query)["context"]
        corrected = request("/api/correct", {"id": original_id, "workspace": WORKSPACE,
                                              "content": CORRECTED, "reason": "installed journey"})
        corrected_id = corrected["id"]
        assert corrected["superseded"] == [original_id]
    with _dashboard(root, env, timeout=timeout, installed=installed) as request:
        current = request(query)
        assert CORRECTED in current["context"] and ORIGINAL not in current["context"]
        past = request(query + "&" + urlencode({"valid_at": historical_at, "known_at": historical_at}))
        assert ORIGINAL in past["context"] and CORRECTED not in past["context"]
        history = request(f"/api/memory/{corrected_id}/history?workspace={WORKSPACE}")
        assert {item["id"] for item in history["versions"]} >= {original_id, corrected_id}
    return ["dashboard HTML", "health/readiness/build identity", "HTTP remember",
            "restart recall", "correction", "restart current and historical recall", "history"]


def run_journey(surface="all", *, timeout=30.0, installed=True):
    if surface not in {"mcp", "server", "all"}:
        raise ValueError("surface must be mcp, server, or all")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive and finite")
    import engraphis
    from engraphis.build_info import package_build_info

    package = Path(engraphis.__file__).resolve()
    if installed and Path(sys.prefix).resolve() not in package.parents:
        raise RuntimeError("the smoke imported checkout code instead of the installed artifact")
    report = {"format": "engraphis-installed-journey/v1", "version": engraphis.__version__,
              "package_source_sha256": package_build_info()["package_source_sha256"],
              "platform": sys.platform, "python": sys.version.split()[0],
              "installed_artifact": installed, "embedding": "deterministic/offline", "checks": {}}
    for name, journey in (("mcp", mcp_journey), ("server", server_journey)):
        if surface in (name, "all"):
            with tempfile.TemporaryDirectory(prefix="engraphis-installed-journey-") as temporary:
                root = Path(temporary).resolve()
                env = isolated_environment(root, installed=installed)
                report["checks"][name] = journey(root, env, timeout=timeout, installed=installed)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--surface", choices=("mcp", "server", "all"), default="all")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = run_journey(args.surface, timeout=args.timeout)
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
