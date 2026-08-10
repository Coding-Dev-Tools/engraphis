#!/usr/bin/env python3
"""Launch the Engraphis WebUI (Inspector + dashboard).

    engraphis-dashboard                        # opens http://127.0.0.1:8700
    engraphis-dashboard --no-open              # starts without opening the browser
    engraphis-dashboard --port 9000            # custom port
    engraphis-dashboard --install-shortcuts    # Desktop + Start Menu icons

The WebUI serves the dashboard single-page app at ``/`` over the v2 engine's
``/api/*`` route set (plus ``/mcp`` when the optional mcp extra is installed).
"""
from __future__ import annotations

import argparse
import errno
import json
import os
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
import webbrowser


_DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
# Windows may report an occupied listener as WSAEACCES (10013) instead of
# WSAEADDRINUSE (10048) when the probe uses SO_REUSEADDR. Treat both as a busy
# port so the health check can distinguish an existing Engraphis server from a
# genuinely unavailable socket.
_ADDRESS_IN_USE_ERRNOS = {errno.EADDRINUSE, errno.EACCES, 10013, 10048}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Keep the occupied-port health probe at the address we just bind-checked."""

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def _embed_model_from_environment() -> str:
    """Use the production model by default, while preserving an explicit offline opt-out."""
    configured = os.environ.get("ENGRAPHIS_EMBED_MODEL")
    return _DEFAULT_EMBED_MODEL if configured is None else configured.strip()


def _run_shortcut_install(silent: bool = False, icon: str = "") -> None:
    cmd = [sys.executable, "-m", "scripts.install_shortcuts"]
    if silent:
        cmd.append("--silent")
    if icon:
        cmd.extend(["--icon", icon])
    import subprocess
    subprocess.run(cmd, check=False)


def _port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be from 1 to 65535")
    return port


def _port_is_available(host: str, port: int) -> bool:
    """Return whether Uvicorn can plausibly bind *host*:*port* right now.

    This intentionally runs before importing ``dashboard_app``.  That import constructs
    the memory service and may load the sentence-transformer model, so discovering an
    already-running dashboard afterwards wastes startup time and looks like a crash.
    The probe cannot eliminate a bind race, but the final error handler repeats it so
    even that race gets an actionable message rather than a generic initialization error.
    """
    addresses = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE,
    )
    for family, socktype, protocol, _canonname, sockaddr in addresses:
        probe = socket.socket(family, socktype, protocol)
        try:
            # Match Uvicorn's bind_socket() configuration.  Without this a recently
            # closed dashboard can leave the probe unable to bind during TIME_WAIT even
            # though the server itself will reuse the address successfully.  This is
            # deliberately SO_REUSEADDR only: the probe still rejects a genuinely
            # unavailable address and never enables concurrent SO_REUSEPORT binding.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(sockaddr)
        except OSError as exc:
            if exc.errno in _ADDRESS_IN_USE_ERRNOS:
                return False
            raise
        finally:
            probe.close()
    return True


def _is_engraphis_dashboard(url: str) -> bool:
    """Check that an existing dashboard is actually ready to serve Ledger.

    ``/api/health`` is intentionally a liveness probe and can remain ``200`` while a
    service worker or the shared Store connection is wedged.  Reusing a process on that
    signal made the launcher preserve a broken dashboard indefinitely.  Readiness is the
    correct occupied-port identity check: it proves the DB and embedder checks completed
    before we tell the caller that an existing dashboard is usable.
    """
    request = urllib.request.Request(
        url.rstrip("/") + "/api/ready", headers={"Accept": "application/json"},
    )
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=0.75) as response:
            raw = response.read(16 * 1024)
    except (OSError, TimeoutError, urllib.error.HTTPError, ValueError):
        return False
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("ready") is True
        and isinstance(payload.get("checks"), dict)
        and all(payload["checks"].get(key) is True for key in ("db", "embedder"))
    )


def _reuse_or_report_occupied_port(
    parser: argparse.ArgumentParser, url: str, *, no_open: bool,
) -> bool:
    """Reuse an existing local dashboard or explain the port conflict and exit."""
    if _is_engraphis_dashboard(url):
        print(f"Engraphis WebUI is already running at {url}.")
        if not no_open:
            try:
                webbrowser.open(url)
            except Exception:
                pass
        return True
    try:
        probe = urllib.request.Request(url.rstrip("/") + "/api/health")
        opener = urllib.request.build_opener(_NoRedirectHandler())
        opener.open(probe, timeout=0.5)
        is_web = True
    except urllib.error.HTTPError:
        is_web = True
    except Exception:
        is_web = False
    if is_web:
        parser.exit(
            1,
            "Error: Cannot start Engraphis WebUI because %s is already in use. "
            "The existing Engraphis dashboard is not ready; restart that process "
            "or choose another port with --port.\n" % url,
        )
    parser.exit(
        1,
        "Error: Cannot start Engraphis WebUI because the port is occupied by a "
        "non-Engraphis service. Stop the other service or choose another port "
        "with --port.\n",
    )


def _startup_error(exc: BaseException, db: str) -> str:
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return ("The server extra is required: pip install \"engraphis[server]\""
                " (needs Python 3.10+)")
    if isinstance(exc, (sqlite3.Error, OSError)):
        if isinstance(exc, sqlite3.OperationalError) and any(
            marker in str(exc).casefold() for marker in ("locked", "busy")
        ):
            return (
                "The Engraphis database is busy while another process is using it. "
                "Close duplicate engraphis-dashboard/engraphis-mcp processes and retry; "
                "the migration will not change the schema until its verified backup "
                "is complete. Database: %s" % db
            )
        return (
            "Could not open the Engraphis database at %s. Check that the path is a "
            "writable SQLite file, then run engraphis-init --check." % db
        )
    if isinstance(exc, RuntimeError):
        return str(exc)
    return "Dashboard initialization failed. Run engraphis-init --check for diagnostics."


def main(argv=None) -> None:
    # Load the owner-private config before argparse snapshots its defaults.  Otherwise
    # a desktop/CLI launch can overwrite trusted embed-model, host, and port
    # values with built-in defaults before dashboard_app is imported, which can turn an
    # offline install or an existing workspace into an apparent startup failure.
    from engraphis import config as _config  # noqa: F401  # loads trusted config once

    ap = argparse.ArgumentParser(description="Start the Engraphis WebUI.")
    ap.add_argument("--host", default=os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"),
                    help="Bind host (default: $ENGRAPHIS_HOST, else 127.0.0.1).")
    # Prefer the platform-injected ``PORT`` (Railway/Fly/Heroku set it and route + health-
    # check to exactly that port). Falling back to ``ENGRAPHIS_PORT`` then 8700 keeps local
    # and docker-compose runs unchanged. Binding a fixed 8700 while the platform expected
    # ``$PORT`` was half of the 2026-07-16 Railway healthcheck failure.
    ap.add_argument("--port", type=_port,
                    default=(os.environ.get("PORT")
                             or os.environ.get("ENGRAPHIS_PORT", "8700")),
                    help="Bind port (default: $PORT, else $ENGRAPHIS_PORT, else 8700).")
    ap.add_argument("--no-open", action="store_true",
                    help="Do not open the browser on startup.")
    ap.add_argument("--reload", action="store_true",
                    help="Reload the v2 server when source files change (development only).")
    ap.add_argument("--install-shortcuts", action="store_true",
                    help="Install desktop and Start Menu shortcuts, then exit.")
    ap.add_argument("--install-shortcuts-silent", action="store_true",
                    help="Same as --install-shortcuts but non-interactive.")
    ap.add_argument("--icon", default="", help="Icon path for shortcuts.")
    args = ap.parse_args(argv)

    if args.install_shortcuts or args.install_shortcuts_silent:
        _run_shortcut_install(silent=args.install_shortcuts_silent, icon=args.icon)
        return

    # netutil (stdlib-only, config-free) keeps this preflight ahead of every dashboard
    # import. It maps
    # a wildcard bind (0.0.0.0/::) to loopback and brackets IPv6 for the printed URL.
    db = os.environ.get("ENGRAPHIS_DB_PATH", "the default user-data location")
    try:
        from engraphis.netutil import display_base_url
        url = display_base_url(args.host, args.port)
        if not _port_is_available(args.host, args.port):
            if _reuse_or_report_occupied_port(parser=ap, url=url, no_open=args.no_open):
                return
    except (OSError, ValueError) as exc:
        ap.exit(1, "Error: Could not check dashboard address %s: %s\n" % (args.host, exc))

    os.environ["ENGRAPHIS_EMBED_MODEL"] = _embed_model_from_environment()
    os.environ["ENGRAPHIS_HOST"] = args.host
    os.environ["ENGRAPHIS_PORT"] = str(args.port)

    try:
        from engraphis import __version__ as _engraphis_version
    except Exception:
        _engraphis_version = "unknown"
    try:
        # Imported AFTER the env writes above: this snapshot and uvicorn's in-process
        # import of the app see the same values, so the banner reports the RESOLVED DB
        # path (installed builds use a per-user data dir, not "./engraphis.db").
        from engraphis.config import settings
        db = settings.db_path
        import uvicorn
        # Uvicorn reload mode must receive an import string, not a preconstructed
        # ASGI object. Avoid importing the app in the parent process in that mode so
        # it does not create a duplicate service/store before the reloader child starts.
        dashboard_app = "engraphis.dashboard_app:app" if args.reload else None
        if dashboard_app is None:
            from engraphis.dashboard_app import app as dashboard_app
        from engraphis.observability import configure_structured_logging
        structured_logs = configure_structured_logging()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - convert startup failures to UX
        ap.exit(1, "Error: %s\n" % _startup_error(exc, db))

    print(f"Engraphis WebUI v{_engraphis_version} - {url}")
    print(f"  Dashboard :  {url}/")
    print(f"  REST API  :  {url}/api")
    print(f"  Database  :  {db}")
    print(f"  Version   :  {_engraphis_version}")
    print("  Press Ctrl+C to stop.")
    sys.stdout.flush()

    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Preserve the actual socket peer. Engraphis validates trusted proxies and consumes
    # the rightmost forwarded hop itself; letting Uvicorn rewrite request.client first
    # destroys that evidence and makes a preseeded X-Forwarded-For spoofable.
    try:
        run_options = {
            "host": args.host,
            "port": args.port,
            "proxy_headers": False,
            "access_log": False,
        }
        if args.reload:
            run_options["reload"] = True
        if structured_logs:
            # Uvicorn's default log_config replaces every uvicorn.access formatter after
            # create_app() installs the redacting JSON formatter. Keeping the existing
            # logging graph is therefore part of the credential-redaction boundary.
            run_options["log_config"] = None
        uvicorn.run(dashboard_app, **run_options)
    except (Exception, SystemExit) as exc:  # noqa: BLE001
        # Uvicorn turns a late bind failure into ``SystemExit(1)`` after it has logged the
        # socket error. Re-probe here so a rare check-to-bind race is still explained.
        try:
            occupied = not _port_is_available(args.host, args.port)
        except OSError:
            occupied = False
        if occupied and _reuse_or_report_occupied_port(
            parser=ap, url=url, no_open=args.no_open,
        ):
            return
        ap.exit(1, "Error: %s\n" % _startup_error(exc, db))


if __name__ == "__main__":
    main()
