"""The restored v1 dashboard — served on the *v2* engine.

Same look-and-feel as the original dashboard (engraphis/static/index.html), but every
route reads/writes the v2 MemoryService where the real data lives. This keeps the v1
server (engraphis/app.py) untouched; run this with `python -m scripts.start_dashboard`.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import hmac
import inspect
import json
import logging
from pathlib import Path, PurePosixPath, PureWindowsPath
from urllib.parse import urlsplit

import os as _os
import secrets
import threading
import time

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engraphis import licensing
from engraphis.config import settings
from engraphis.core.documents import supported_document_extensions
from engraphis.http_security import wants_https
from engraphis.local_auth import (
    BROWSER_SESSION_COOKIE,
    BROWSER_SESSION_SECONDS,
    bearer_ok,
    browser_session,
    browser_session_ok,
    token_ok,
)
from engraphis.routes import v2_api
from engraphis.service import (
    MAX_IMPORT_FILES,
    MAX_IMPORT_RESOURCE_BYTES,
    MAX_IMPORT_TOTAL_BYTES,
    MemoryService,
)

logger = logging.getLogger("engraphis")

_STATIC = Path(__file__).resolve().parent / "static"
_CLASSIC_ASSETS = Path(__file__).resolve().parent / "classic_assets"
_V2_ASSETS = Path(__file__).resolve().parent / "dashboard_assets"
_INDEX = _V2_ASSETS / "index.html"

_DASHBOARD_JSON_REQUEST_BYTES = 8 * 1024 * 1024
_DASHBOARD_UPLOAD_REQUEST_BYTES = (
    MAX_IMPORT_TOTAL_BYTES + MAX_IMPORT_FILES * 4096
)
_DASHBOARD_REQUEST_BODY_LIMITS = {
    "/api/auth/session": 8 * 1024,
    "/api/workspaces/import-files": _DASHBOARD_UPLOAD_REQUEST_BYTES,
    "/api/workspaces/import-obsidian/preview": _DASHBOARD_UPLOAD_REQUEST_BYTES,
    "/api/workspaces/import-obsidian/run": _DASHBOARD_UPLOAD_REQUEST_BYTES,
    "/api/workspaces/import-documents/preview": _DASHBOARD_UPLOAD_REQUEST_BYTES,
    "/api/workspaces/import-documents/run": _DASHBOARD_UPLOAD_REQUEST_BYTES,
}

# The dashboard accepts only documents it can hand straight to the local importer.
# It deliberately does not upload arbitrary binary files.  Obsidian is the one
# exception: its attachments are represented as a content-free manifest, so note
# links can be reported without keeping a second copy of attachment bytes.
_DOCUMENT_SUFFIXES = frozenset(supported_document_extensions())


class _RequestBodyTooLarge(Exception):
    """Internal signal raised once the streaming request ceiling is crossed."""


class _RequestBodyLimitMiddleware:
    """Reject oversized request streams before FastAPI parses or buffers the body."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "PUT", "PATCH", "DELETE"}
        ):
            await self.app(scope, receive, send)
            return
        max_bytes = _DASHBOARD_REQUEST_BODY_LIMITS.get(
            scope.get("path", ""), _DASHBOARD_JSON_REQUEST_BYTES
        )
        raw_lengths = [
            value for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if len(raw_lengths) > 1:
            await JSONResponse(
                {"error": "invalid content-length"}, status_code=400
            )(scope, receive, send)
            return
        if raw_lengths:
            try:
                declared_length = int(raw_lengths[0])
            except (TypeError, ValueError):
                declared_length = -1
            if declared_length < 0:
                await JSONResponse(
                    {"error": "invalid content-length"}, status_code=400
                )(scope, receive, send)
                return
            if declared_length > max_bytes:
                await self._too_large(scope, receive, send, max_bytes)
                return

        received = 0
        limit_exceeded = False

        async def limited_receive():
            nonlocal limit_exceeded, received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    limit_exceeded = True
                    raise _RequestBodyTooLarge
            return message

        async def guarded_send(message):
            if not limit_exceeded:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _RequestBodyTooLarge:
            pass
        if limit_exceeded:
            await self._too_large(scope, receive, send, max_bytes)

    @staticmethod
    async def _too_large(scope, receive, send, max_bytes):
        await JSONResponse(
            {"error": "request body too large", "max_bytes": max_bytes},
            status_code=413,
        )(scope, receive, send)


async def _dashboard_consolidation_loop(service: MemoryService) -> None:
    """Run opt-in v2 consolidation from the dashboard's actual lifespan.

    The retired compatibility app owns the historical consciousness loop, but the
    supported dashboard is the process that serves the v2 MemoryService. Keep this
    maintenance task v2-only and dispatch both the candidate scan and SQLite writes to
    worker threads so request handling never shares the event loop with a sweep.
    """
    from engraphis.app import _consolidation_candidates_exist, _run_loop_consolidation

    ticks = 0
    while True:
        try:
            await asyncio.sleep(settings.loop_interval)
            ticks += 1
            interval = int(settings.loop_consolidate)
            if interval <= 0 or ticks % interval:
                continue
            if not await asyncio.to_thread(_consolidation_candidates_exist, service.engine):
                continue
            await asyncio.to_thread(_run_loop_consolidation, service.engine)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - maintenance must not kill the server
            logger.error("Dashboard consolidation loop error (%s)", type(exc).__name__)


class _FreshStaticFiles(StaticFiles):
    """Revalidate local dashboard assets so a running UI cannot pin an old renderer.

    The HTML shells are already ``no-store``, but their JS/CSS dependencies previously
    inherited StaticFiles' cacheable response.  That made an unchanged query string keep
    an older graph engine alive after a source/package update.
    """

    @staticmethod
    def _is_private_asset(path: str) -> bool:
        """Keep package implementation files out of the public asset mounts."""
        parts = [part for part in path.replace("\\", "/").split("/") if part]
        return (
            any(part == "__pycache__" or part.startswith(".") for part in parts)
            or any(
                part.lower().endswith((".py", ".pyc", ".pyo", ".pyi"))
                for part in parts
            )
        )

    async def get_response(self, path, scope):
        if self._is_private_asset(path):
            return Response(status_code=404)
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


# The public package is a single-user local runtime. Hosted account, Team, trial, and
# recovery endpoints live in Engraphis Cloud; only the shell and health/auth metadata are
# reachable before the optional local API token gate.
_PUBLIC = {
    "/",
    "/api/health",
    "/api/ready",
    "/api/auth/state",
    "/api/auth/session",
}


class _BrowserSessionReq(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


class _DashboardApprovalReq(BaseModel):
    """Human-review request accepted only by the browser dashboard ceremony."""

    memory_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


_REVIEW_CSRF_HEADER = "X-Engraphis-Review-CSRF"
_DOCUMENT_REVIEW_TTL_SECONDS = 5 * 60
_DOCUMENT_REVIEW_LIMIT = 256


def _embedder_status(embedder, configured_model: str) -> str:
    """Concise startup status without misdiagnosing an explicit offline selection."""
    from engraphis.backends.embedder_deterministic import DeterministicEmbedder

    if not isinstance(embedder, DeterministicEmbedder):
        return "semantic search ready"
    if not configured_model:
        return "deterministic offline mode selected"
    return "configured model unavailable; deterministic fallback active"


def _mcp_transport_security(mcp):
    """Keep the SDK's DNS-rebinding guard and add this deployment's public URL."""
    from mcp.server.transport_security import TransportSecuritySettings

    current = mcp.settings.transport_security
    allowed_hosts = set(current.allowed_hosts)
    allowed_origins = set(current.allowed_origins)
    dashboard_url = _os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip()
    if dashboard_url:
        parsed = urlsplit(dashboard_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ValueError("ENGRAPHIS_DASHBOARD_URL must be an http(s) URL without userinfo")
        from engraphis.netutil import bracket_host
        host = bracket_host(parsed.hostname)
        if parsed.port is not None:
            host = "%s:%d" % (host, parsed.port)
        allowed_hosts.add(host)
        allowed_origins.add("%s://%s" % (parsed.scheme, host))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(allowed_hosts),
        allowed_origins=sorted(allowed_origins),
    )


def create_app() -> FastAPI:
    from engraphis.observability import configure_structured_logging
    configure_structured_logging()
    # MCP-over-HTTP agent connect: build the streamable-http ASGI app up front so we can
    # give the dashboard a lifespan that initializes its session manager (a mounted
    # sub-app's own lifespan does NOT run in Starlette - only the root app's does -
    # which is why a naive app.mount('/mcp', mcp.streamable_http_app()) raises
    # 'Task group is not initialized'). The endpoint is built at '/' inside the sub-app
    # so mounting under /mcp lines up (Starlette strips the mount prefix).
    import importlib.util as _importlib_util
    import contextlib as _contextlib
    _mcp_asgi = None
    _mcp_mgr = None
    try:
        if _importlib_util.find_spec("mcp") is None:
            raise ImportError("the optional mcp package is not installed")
        import engraphis.mcp_server as _mcp_mod
        # The MCP session manager's run() is once-per-instance, but create_app() may be
        # called more than once in a process (tests, re-import). Reset the lazily-created
        # manager so each app gets a fresh, runnable one. No-op for the first call.
        try:
            _mcp_mod.mcp._session_manager = None
        except Exception:  # noqa: BLE001 - private attr; stay robust across mcp versions
            pass
        _prev_path = _mcp_mod.mcp.settings.streamable_http_path
        _prev_security = _mcp_mod.mcp.settings.transport_security
        try:
            _mcp_mod.mcp.settings.streamable_http_path = "/"
            _mcp_mod.mcp.settings.transport_security = _mcp_transport_security(_mcp_mod.mcp)
            _mcp_asgi = _mcp_mod.mcp.streamable_http_app()
        finally:
            # streamable_http_app() captures these settings in its session manager. Restore
            # the global FastMCP instance so importing the dashboard cannot alter the
            # standalone MCP server in the same process.
            _mcp_mod.mcp.settings.streamable_http_path = _prev_path
            _mcp_mod.mcp.settings.transport_security = _prev_security
        _mcp_mgr = _mcp_mod.mcp.session_manager
    except (Exception, SystemExit) as _exc:  # noqa: BLE001 - MCP mount stays optional
        import logging as _logging
        # A server-only install intentionally has no MCP SDK; that expected shape stays
        # silent. If an installed SDK fails to mount, retain a warning for operators.
        _level = _logging.INFO if importlib.util.find_spec("mcp") is None else _logging.WARNING
        _logging.getLogger("engraphis").log(
            _level, "MCP /mcp mount skipped (%s)", type(_exc).__name__
        )

    @_contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI):
        background_task = None
        try:  # one-line "update available" notice (background, fail-silent, opt-out)
            import logging as _logging

            from engraphis import update_check
            update_check.emit_startup_notice(_logging.getLogger("engraphis").info)
        except Exception:  # noqa: BLE001 - never block dashboard startup
            pass
        if settings.loop_interval > 0 and settings.loop_consolidate > 0:
            background_task = asyncio.create_task(_dashboard_consolidation_loop(svc))
            logger.info(
                "Dashboard consolidation loop started (interval=%ds)",
                settings.loop_interval,
            )
        try:
            if _mcp_asgi is not None and _mcp_mgr is not None:
                async with _mcp_mgr.run():
                    yield
            else:
                yield
        finally:
            try:
                if background_task is not None:
                    background_task.cancel()
                    try:
                        await background_task
                    except asyncio.CancelledError:
                        pass
            finally:
                await asyncio.to_thread(v2_api.release_service, svc)

    # FastAPI's interactive docs execute CDN-hosted JavaScript with same-origin
    # authority. Do not expose that supply-chain surface on an authenticated memory
    # dashboard; the machine-readable schema remains available behind the normal gate.
    app = FastAPI(title="Engraphis Dashboard", docs_url=None, redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=_lifespan)
    app.state.mcp_over_http = _mcp_asgi is not None
    app.add_middleware(_RequestBodyLimitMiddleware)

    # Honour the advertised allow-list on the actual GA dashboard entrypoint.  A
    # wildcard can never carry browser credentials.
    _cors_wildcard = "*" in settings.cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=not _cors_wildcard,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(licensing.LicenseError)
    async def _license_error(request: Request, exc: licensing.LicenseError):
        feature = exc.feature or "team"
        tier = licensing.required_plan(feature)
        # Derive the destination from the tier that was actually required. Calling
        # upgrade_url() with no argument resolves plan="pro", so a Team-gated feature
        # produced tier_required="team" alongside the Pro checkout link.
        body = {
            "error": str(exc),
            "upgrade": True,
            "feature": feature,
            "tier_required": tier,
            "upgrade_url": licensing.upgrade_url(tier),
            "purchase_url": licensing.upgrade_url(tier),
        }
        return JSONResponse({**body, "detail": body}, status_code=402)
    svc = MemoryService.create(
        settings.db_path, embed_model=settings.embed_model,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim if settings.embed_dim is not None else 384,
        vector_backend=settings.vector_backend,
        rerank_model=getattr(settings, "rerank_model", "") or None,
        rerank_revision=getattr(settings, "rerank_revision", "") or None,
        allowed_workspaces=settings.allowed_workspaces)

    def _discard_unbound_service() -> None:
        try:
            svc.close()
        except Exception as exc:  # noqa: BLE001 - preserve the startup root cause
            logger.error("failed to close rejected dashboard service (%s)", type(exc).__name__)

    app.state.service = svc
    # Startup self-check: verify critical Store methods exist and stats() works.
    # Catches merge-conflict regressions where methods escape the Store class body
    # (e.g. orphaned at module level after a bad rebase). Fails fast with a clear
    # error instead of silently serving 500s on /api/bootstrap.
    _required_store_methods = (
        "prompt_eligibility_counts",
        "embedding_space_health",
        "active_embedding_space",
        "begin_embedding_rebuild",
        "finish_embedding_rebuild",
    )
    _store_cls = type(svc.store)
    _missing = [m for m in _required_store_methods if not hasattr(_store_cls, m)]
    if _missing:
        _discard_unbound_service()
        raise RuntimeError(
            f"Engraphis Store class is missing required methods: {', '.join(_missing)}. "
            f"This usually means methods were accidentally moved outside the Store class "
            f"body during a merge conflict. Check engraphis/core/store.py."
        )
    try:
        # When the instance is bound to allowed_workspaces, stats() requires
        # a workspace argument. Use the first allowed workspace for the self-check.
        if svc.allowed_workspaces is not None:
            svc.stats(workspace=next(iter(svc.allowed_workspaces)))
        else:
            svc.stats()
    except AttributeError as exc:
        _discard_unbound_service()
        raise RuntimeError(
            f"Engraphis service startup self-check failed: {exc}. "
            f"Store method is likely orphaned outside the class body."
        ) from exc
    except BaseException:
        _discard_unbound_service()
        raise
    # The review token is intentionally process-local and is never a general API
    # credential. It is minted alongside a short-lived browser session and exists only
    # to authorize the narrowly scoped human-approval dashboard action below.
    app.state.review_csrf_tokens = {}
    # A document preview authorizes exactly one subsequent import of the reviewed
    # bytes into the reviewed target.  Records contain only a request digest, the
    # already-opaque browser-session value, and a short process-local expiry.
    app.state.document_import_reviews = {}
    app.state.document_import_review_lock = threading.Lock()
    try:
        import sys as _sys
        _ed = svc.engine.embedder
        print("[engraphis] embedder: %s dim=%s (%s)" % (
            type(_ed).__name__, getattr(_ed, "dim", "?"),
            _embedder_status(_ed, settings.embed_model)), file=_sys.stderr)
    except Exception:
        pass
    try:
        v2_api.set_service(svc)
    except BaseException:
        _discard_unbound_service()
        raise
    app.include_router(v2_api.router)

    app.state.auth_store = None
    app.state.team_enabled = False

    @app.get("/api/auth/state", include_in_schema=False)
    def local_auth_state():
        """Describe the local token gate without exposing hosted Team endpoints."""
        return {
            "enabled": bool(settings.api_token),
            "mode": "local-token" if settings.api_token else "open",
            "user": None,
            "hosted_team": True,
            "cloud_url": licensing.upgrade_url("team"),
        }

    @app.post("/api/auth/session", include_in_schema=False)
    def open_browser_session(req: _BrowserSessionReq, request: Request):
        """Exchange the deployment token for a short-lived HttpOnly browser cookie.

        The bearer is never put in local/session storage. The dashboard holds it only for
        this same-origin POST, then every API request uses the signed cookie plus a custom
        request header that ordinary cross-site forms cannot forge.
        """

        if not settings.api_token:
            return JSONResponse(
                {"error": "local API authentication is not configured"},
                status_code=409,
            )
        if not token_ok(req.token, settings.api_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        session_value = browser_session(settings.api_token)
        review_csrf_token = secrets.token_urlsafe(32)
        # A dashboard restart deliberately invalidates this token. Keep only the latest
        # token for each session value; unlike an API bearer, it has no authority by
        # itself and is not persisted to disk or browser storage.
        app.state.review_csrf_tokens[session_value] = review_csrf_token
        response = JSONResponse(
            {"authenticated": True, "review_csrf_token": review_csrf_token}
        )
        response.headers["Cache-Control"] = "no-store"
        response.set_cookie(
            BROWSER_SESSION_COOKIE,
            session_value,
            max_age=BROWSER_SESSION_SECONDS,
            httponly=True,
            secure=wants_https(request),
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/dashboard/review/approve", include_in_schema=False)
    def dashboard_review_approve(req: _DashboardApprovalReq, request: Request):
        """Approve one record from the authenticated browser review surface.

        This is intentionally *not* a v2 API or MCP operation.  A bearer token cannot
        invoke it: the caller must hold the HttpOnly browser session and echo the
        per-session CSRF value returned only by the same-origin login exchange.  The
        private hosted service owns owner/admin approval for hosted deployments.
        """

        if not settings.api_token:
            return JSONResponse(
                {"error": "dashboard approval requires ENGRAPHIS_API_TOKEN"},
                status_code=409,
            )
        session_value = request.cookies.get(BROWSER_SESSION_COOKIE)
        if not browser_session_ok(session_value, settings.api_token):
            return JSONResponse({"error": "browser session required"}, status_code=401)
        if request.headers.get("X-Engraphis-Browser-Session") != "1":
            return JSONResponse({"error": "browser session header required"}, status_code=403)
        expected = app.state.review_csrf_tokens.get(session_value)
        supplied = request.headers.get(_REVIEW_CSRF_HEADER, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            return JSONResponse({"error": "review CSRF confirmation required"}, status_code=403)
        reason = req.reason.strip()
        if not reason:
            return JSONResponse({"error": "review reason required"}, status_code=422)
        source = svc.store.get_memory(req.memory_id)
        if source is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        workspace = svc.store.conn.execute(
            "SELECT name FROM workspaces WHERE id=?", (source.workspace_id,),
        ).fetchone()
        if workspace is None:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        try:
            # Approval accepts only an opaque memory id, so recover the authoritative
            # workspace from the source and run the same allow-list guard as every
            # service-level workspace operation before the engine creates a successor.
            svc._authorize_workspace(workspace["name"])
        except ValueError:
            return JSONResponse({"error": "workspace approval is not permitted"}, status_code=403)
        try:
            result = svc.engine.approve_for_prompt(
                req.memory_id,
                reviewer="dashboard_browser_session",
                reason=reason,
            )
        except KeyError:
            return JSONResponse({"error": "memory not found"}, status_code=404)
        except ValueError:
            # Do not expose a governed record's content or arbitrary engine exception.
            return JSONResponse({"error": "approval was rejected"}, status_code=409)
        response = JSONResponse({"approved": True, **result})
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/dashboard/review/csrf", include_in_schema=False)
    def dashboard_review_csrf(request: Request):
        """Return the in-memory CSRF value for an already-authenticated dashboard."""

        if not settings.api_token:
            return JSONResponse(
                {"error": "dashboard approval requires ENGRAPHIS_API_TOKEN"},
                status_code=409,
            )
        session_value = request.cookies.get(BROWSER_SESSION_COOKIE)
        if not browser_session_ok(session_value, settings.api_token):
            return JSONResponse({"error": "browser session required"}, status_code=401)
        if request.headers.get("X-Engraphis-Browser-Session") != "1":
            return JSONResponse({"error": "browser session header required"}, status_code=403)
        token = app.state.review_csrf_tokens.get(session_value)
        if not token:
            token = secrets.token_urlsafe(32)
            app.state.review_csrf_tokens[session_value] = token
        response = JSONResponse({"review_csrf_token": token})
        response.headers["Cache-Control"] = "no-store"
        return response

    def _require_document_browser_owner(request: Request) -> str:
        """Keep local document uploads off generic bearer-token API/MCP surfaces.

        A browser owner must hold the HttpOnly dashboard session *and* echo its
        process-local CSRF nonce.  The general API middleware deliberately accepts a
        bearer for automation clients; that authority is insufficient to upload a
        selected local documents or make their imported content trusted.
        """
        if not settings.api_token:
            raise HTTPException(
                status_code=409,
                detail={"error": "document import requires ENGRAPHIS_API_TOKEN"},
            )
        session_value = request.cookies.get(BROWSER_SESSION_COOKIE)
        if not browser_session_ok(session_value, settings.api_token):
            raise HTTPException(status_code=401, detail={"error": "browser session required"})
        if request.headers.get("X-Engraphis-Browser-Session") != "1":
            raise HTTPException(status_code=403, detail={"error": "browser session header required"})
        expected = app.state.review_csrf_tokens.get(session_value)
        supplied = request.headers.get(_REVIEW_CSRF_HEADER, "")
        if not expected or not hmac.compare_digest(supplied, expected):
            raise HTTPException(status_code=403, detail={"error": "owner confirmation required"})
        # Include the process-local per-login nonce, not only the signed cookie.
        # Two owner logins minted in the same second can otherwise share the same
        # deterministic cookie value. Rotating the owner confirmation must also
        # invalidate any outstanding import review minted by the earlier login.
        return hashlib.sha256(
            f"{session_value}\0{expected}".encode("utf-8"),
        ).hexdigest()

    def _document_review_digest(
        *, uploads: list[tuple[str, bytes]], attachments: list[dict],
        workspace: str, repo: str, session_id: str, scope: str,
        memory_type: str, source_id: str, source_label: str,
        on_conflict: str, source_mode: str,
    ) -> str:
        """Bind one preview to its exact local bytes, provenance, and write target."""

        material = {
            "attachments": sorted(
                (
                    {"path": str(item["path"]), "size": int(item["size"])}
                    for item in attachments
                ),
                key=lambda item: (item["path"].casefold(), item["path"]),
            ),
            "files": sorted(
                (
                    {
                        "path": path,
                        "size": len(raw),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                    }
                    for path, raw in uploads
                ),
                key=lambda item: (item["path"].casefold(), item["path"]),
            ),
            "target": {
                "memory_type": str(memory_type),
                "on_conflict": str(on_conflict),
                "repo": str(repo),
                "scope": str(scope),
                "session_id": str(session_id),
                "source_id": str(source_id),
                "source_label": str(source_label),
                "source_mode": str(source_mode),
                "workspace": str(workspace),
            },
        }
        encoded = json.dumps(
            material, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _prune_document_reviews(now: float) -> None:
        reviews = app.state.document_import_reviews
        for token, record in list(reviews.items()):
            if float(record["expires_at"]) <= now:
                reviews.pop(token, None)
        while len(reviews) >= _DOCUMENT_REVIEW_LIMIT:
            oldest = min(
                reviews, key=lambda token: float(reviews[token]["expires_at"]),
            )
            reviews.pop(oldest, None)

    def _issue_document_review(
        report: dict, *, owner_binding: str, digest: str,
    ) -> dict:
        now = time.monotonic()
        token = secrets.token_urlsafe(32)
        with app.state.document_import_review_lock:
            _prune_document_reviews(now)
            app.state.document_import_reviews[token] = {
                "owner_binding": owner_binding,
                "digest": digest,
                "expires_at": now + _DOCUMENT_REVIEW_TTL_SECONDS,
            }
        response = dict(report)
        response["review_token"] = token
        response["review_expires_in"] = _DOCUMENT_REVIEW_TTL_SECONDS
        return response

    def _consume_document_review(
        token: str, *, owner_binding: str, digest: str,
    ) -> None:
        supplied = str(token or "")
        if not 32 <= len(supplied) <= 200:
            raise HTTPException(
                status_code=403,
                detail={"error": "a fresh matching import preview is required"},
            )
        now = time.monotonic()
        with app.state.document_import_review_lock:
            _prune_document_reviews(now)
            record = app.state.document_import_reviews.pop(supplied, None)
        if (
            record is None
            or not hmac.compare_digest(
                str(record["owner_binding"]), owner_binding,
            )
            or not hmac.compare_digest(str(record["digest"]), digest)
        ):
            raise HTTPException(
                status_code=403,
                detail={"error": "a fresh matching import preview is required"},
            )

    def _document_relative_path(value: object) -> str:
        raw = str(value or "").replace("\\", "/")
        candidate = PurePosixPath(raw)
        windows = PureWindowsPath(raw)
        if (
            not raw
            or "\x00" in raw
            or candidate.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or any(part in {"", ".", ".."} for part in candidate.parts)
            or any(ord(character) < 32 for character in raw)
        ):
            raise HTTPException(status_code=400, detail={"error": "invalid upload path"})
        return candidate.as_posix()

    async def _document_uploads(
        files: list[UploadFile], *, source_mode: str,
    ) -> list[tuple[str, bytes]]:
        if not files or len(files) > MAX_IMPORT_FILES:
            raise HTTPException(status_code=413, detail={"error": "invalid document file count"})
        uploads: list[tuple[str, bytes]] = []
        seen_paths: set[str] = set()
        total = 0
        for upload in files:
            relative_path = _document_relative_path(upload.filename)
            path_key = relative_path.casefold()
            if path_key in seen_paths:
                raise HTTPException(status_code=400, detail={"error": "duplicate upload path"})
            seen_paths.add(path_key)
            suffix = PurePosixPath(relative_path).suffix.lower()
            if source_mode == "obsidian":
                if suffix != ".md":
                    raise HTTPException(
                        status_code=400,
                        detail={"error": "Obsidian mode accepts Markdown note bytes only"},
                    )
            elif suffix not in _DOCUMENT_SUFFIXES:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "unsupported document format"},
                )
            raw = await upload.read(MAX_IMPORT_RESOURCE_BYTES + 1)
            if len(raw) > MAX_IMPORT_RESOURCE_BYTES:
                raise HTTPException(status_code=413, detail={"error": "document file is too large"})
            total += len(raw)
            if total > MAX_IMPORT_TOTAL_BYTES:
                raise HTTPException(status_code=413, detail={"error": "document upload is too large"})
            uploads.append((relative_path, raw))
        return uploads

    def _document_attachments(raw_manifest: str, *, source_mode: str) -> list[dict]:
        if source_mode != "obsidian" and raw_manifest not in {"", "[]", None}:
            raise HTTPException(
                status_code=400,
                detail={"error": "attachment manifests are only supported in Obsidian mode"},
            )
        try:
            manifest = json.loads(raw_manifest or "[]")
        except (TypeError, ValueError, RecursionError) as exc:
            raise HTTPException(status_code=400, detail={"error": "invalid attachment manifest"}) from exc
        if not isinstance(manifest, list) or len(manifest) > MAX_IMPORT_FILES * 20:
            raise HTTPException(status_code=400, detail={"error": "invalid attachment manifest"})
        safe = []
        seen_paths: set[str] = set()
        for entry in manifest:
            if not isinstance(entry, dict):
                raise HTTPException(status_code=400, detail={"error": "invalid attachment manifest"})
            path = _document_relative_path(entry.get("path"))
            path_key = path.casefold()
            if path_key in seen_paths:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "duplicate attachment path"},
                )
            seen_paths.add(path_key)
            size = entry.get("size")
            if type(size) is not int or not 0 <= size <= MAX_IMPORT_RESOURCE_BYTES:
                raise HTTPException(status_code=400, detail={"error": "invalid attachment manifest"})
            safe.append({"path": path, "size": size})
        return safe

    def _reject_document_path_overlap(
        uploads: list[tuple[str, bytes]], attachments: list[dict],
    ) -> None:
        uploaded = {path.casefold() for path, _raw in uploads}
        if uploaded.intersection(str(item["path"]).casefold() for item in attachments):
            raise HTTPException(
                status_code=400,
                detail={"error": "upload and attachment paths overlap"},
            )

    def _document_source_identity(source_id: str, source_label: str) -> tuple[str, str]:
        """Require an explicit identity before creating a browser-upload source."""
        clean_id = source_id.strip()
        clean_label = source_label.strip()
        if not clean_id and not clean_label:
            raise HTTPException(
                status_code=400,
                detail={"error": "source label is required for a new source"},
            )
        return clean_id, clean_label

    def _document_service_call(
        generic_name: str, legacy_name: str, *, generic_kwargs: dict,
        legacy_kwargs: dict,
    ):
        """Call the universal facade when available, retaining old local databases.

        Service rollout is intentionally independent from dashboard rollout.  Filter
        arguments for an explicit service signature so an older local service keeps
        serving its Obsidian compatibility routes during a staged upgrade.
        """
        method = getattr(svc, generic_name, None)
        kwargs = generic_kwargs if method is not None else legacy_kwargs
        if method is None:
            method = getattr(svc, legacy_name)
        signature = inspect.signature(method)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        if not accepts_kwargs:
            kwargs = {
                name: value for name, value in kwargs.items()
                if name in signature.parameters
            }
        return method(**kwargs)

    def _document_sources(workspace: str):
        method = getattr(svc, "list_source_vaults", None)
        if method is None:
            method = getattr(svc, "list_document_sources", None)
        if method is None:
            method = getattr(svc, "list_obsidian_vaults")
        return method(workspace)

    @app.get("/api/workspaces/import-documents/sources", include_in_schema=False)
    def document_sources(workspace: str, request: Request):
        _require_document_browser_owner(request)
        try:
            return {"sources": _document_sources(workspace)}
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None

    @app.get("/api/workspaces/import-documents/formats", include_in_schema=False)
    def document_formats(request: Request):
        """Expose the local parser registry to the owner-only browser wizard.

        This avoids treating the client-side picker as an authority while keeping
        its supported-format hint synchronized with the server that validates every
        uploaded byte.
        """
        _require_document_browser_owner(request)
        return {"extensions": sorted(_DOCUMENT_SUFFIXES)}

    @app.post("/api/workspaces/import-documents/preview", include_in_schema=False)
    async def document_preview(
        request: Request,
        workspace: str = Form(...), repo: str = Form(""), session_id: str = Form(""),
        scope: str = Form("workspace"), memory_type: str = Form("semantic"),
        source_id: str = Form(""), source_label: str = Form(""), on_conflict: str = Form("error"),
        source_mode: str = Form("documents"),
        confirmed: str = Form("false"), attachment_manifest: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ):
        owner_binding = _require_document_browser_owner(request)
        if source_mode not in {"documents", "obsidian"}:
            raise HTTPException(status_code=400, detail={"error": "invalid document source mode"})
        source_id, source_label = _document_source_identity(source_id, source_label)
        uploads = await _document_uploads(files, source_mode=source_mode)
        attachments = _document_attachments(attachment_manifest, source_mode=source_mode)
        _reject_document_path_overlap(uploads, attachments)
        try:
            report = _document_service_call(
                "preview_document_upload", "preview_obsidian_upload",
                generic_kwargs={
                    "files": uploads, "attachment_manifest": attachments,
                    "workspace": workspace, "repo": repo or None,
                    "session_id": session_id or None, "scope": scope,
                    "memory_type": memory_type, "source_id": source_id or None,
                    "source_label": source_label, "on_conflict": on_conflict,
                },
                legacy_kwargs={
                    "files": uploads, "attachment_manifest": attachments,
                    "workspace": workspace, "repo": repo or None,
                    "session_id": session_id or None, "scope": scope,
                    "memory_type": memory_type, "vault_id": source_id or None,
                    "vault_label": source_label, "on_conflict": on_conflict,
                    "confirmed": confirmed.strip().lower() == "true",
                },
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None
        digest = _document_review_digest(
            uploads=uploads, attachments=attachments, workspace=workspace,
            repo=repo, session_id=session_id, scope=scope,
            memory_type=memory_type, source_id=source_id,
            source_label=source_label, on_conflict=on_conflict,
            source_mode=source_mode,
        )
        return _issue_document_review(
            report, owner_binding=owner_binding, digest=digest,
        )

    @app.post("/api/workspaces/import-documents/run", include_in_schema=False)
    async def document_run(
        request: Request,
        workspace: str = Form(...), repo: str = Form(""), session_id: str = Form(""),
        scope: str = Form("workspace"), memory_type: str = Form("semantic"),
        source_id: str = Form(""), source_label: str = Form(""), on_conflict: str = Form("error"),
        source_mode: str = Form("documents"),
        confirmed: str = Form("false"), review_token: str = Form(""),
        attachment_manifest: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ):
        owner_binding = _require_document_browser_owner(request)
        if confirmed.strip().lower() != "true":
            raise HTTPException(status_code=403, detail={"error": "owner confirmation required"})
        if source_mode not in {"documents", "obsidian"}:
            raise HTTPException(status_code=400, detail={"error": "invalid document source mode"})
        source_id, source_label = _document_source_identity(source_id, source_label)
        uploads = await _document_uploads(files, source_mode=source_mode)
        attachments = _document_attachments(attachment_manifest, source_mode=source_mode)
        _reject_document_path_overlap(uploads, attachments)
        digest = _document_review_digest(
            uploads=uploads, attachments=attachments, workspace=workspace,
            repo=repo, session_id=session_id, scope=scope,
            memory_type=memory_type, source_id=source_id,
            source_label=source_label, on_conflict=on_conflict,
            source_mode=source_mode,
        )
        _consume_document_review(
            review_token, owner_binding=owner_binding, digest=digest,
        )
        try:
            return _document_service_call(
                "import_document_upload", "import_obsidian_upload",
                generic_kwargs={
                    "files": uploads, "attachment_manifest": attachments,
                    "workspace": workspace, "repo": repo or None,
                    "session_id": session_id or None, "scope": scope,
                    "memory_type": memory_type, "source_id": source_id or None,
                    "source_label": source_label, "on_conflict": on_conflict,
                    "confirmed": True,
                },
                legacy_kwargs={
                    "files": uploads, "attachment_manifest": attachments,
                    "workspace": workspace, "repo": repo or None,
                    "session_id": session_id or None, "scope": scope,
                    "memory_type": memory_type, "vault_id": source_id or None,
                    "vault_label": source_label, "on_conflict": on_conflict,
                    "confirmed": True,
                },
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None

    @app.get("/api/workspaces/import-documents/jobs/{job_id}", include_in_schema=False)
    def document_job(job_id: str, workspace: str, request: Request):
        _require_document_browser_owner(request)
        try:
            return _document_service_call(
                "get_document_import_job", "get_obsidian_import_job",
                generic_kwargs={"job_id": job_id, "workspace": workspace},
                legacy_kwargs={"job_id": job_id, "workspace": workspace},
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=404, detail={"error": "import job not found"}) from None

    @app.post("/api/workspaces/import-documents/jobs/{job_id}/cancel", include_in_schema=False)
    def cancel_document_job(job_id: str, request: Request, workspace: str = Form(...)):
        _require_document_browser_owner(request)
        try:
            return _document_service_call(
                "cancel_document_import_job", "cancel_obsidian_import_job",
                generic_kwargs={"job_id": job_id, "workspace": workspace},
                legacy_kwargs={"job_id": job_id, "workspace": workspace},
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=404, detail={"error": "import job not found"}) from None

    # The short-lived Obsidian routes remain browser-owner-only compatibility aliases
    # for saved dashboard links.  The universal wizard uses import-documents above.
    @app.get("/api/workspaces/import-obsidian/vaults", include_in_schema=False)
    def obsidian_vaults(workspace: str, request: Request):
        _require_document_browser_owner(request)
        try:
            return {"vaults": svc.list_obsidian_vaults(workspace)}
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None

    @app.post("/api/workspaces/import-obsidian/preview", include_in_schema=False)
    async def obsidian_preview_alias(
        request: Request, workspace: str = Form(...), repo: str = Form(""),
        session_id: str = Form(""), scope: str = Form("workspace"),
        memory_type: str = Form("semantic"), vault_id: str = Form(""),
        vault_label: str = Form(""), on_conflict: str = Form("error"),
        confirmed: str = Form("false"), attachment_manifest: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ):
        owner_binding = _require_document_browser_owner(request)
        vault_id, vault_label = _document_source_identity(vault_id, vault_label)
        uploads = await _document_uploads(files, source_mode="obsidian")
        attachments = _document_attachments(attachment_manifest, source_mode="obsidian")
        _reject_document_path_overlap(uploads, attachments)
        try:
            report = svc.preview_obsidian_upload(
                files=uploads, attachment_manifest=attachments, workspace=workspace,
                repo=repo or None, session_id=session_id or None, scope=scope,
                memory_type=memory_type, vault_id=vault_id or None,
                vault_label=vault_label, on_conflict=on_conflict,
                confirmed=confirmed.strip().lower() == "true",
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None
        digest = _document_review_digest(
            uploads=uploads, attachments=attachments, workspace=workspace,
            repo=repo, session_id=session_id, scope=scope,
            memory_type=memory_type, source_id=vault_id,
            source_label=vault_label, on_conflict=on_conflict,
            source_mode="obsidian",
        )
        return _issue_document_review(
            report, owner_binding=owner_binding, digest=digest,
        )

    @app.post("/api/workspaces/import-obsidian/run", include_in_schema=False)
    async def obsidian_run_alias(
        request: Request, workspace: str = Form(...), repo: str = Form(""),
        session_id: str = Form(""), scope: str = Form("workspace"),
        memory_type: str = Form("semantic"), vault_id: str = Form(""),
        vault_label: str = Form(""), on_conflict: str = Form("error"),
        confirmed: str = Form("false"), review_token: str = Form(""),
        attachment_manifest: str = Form("[]"),
        files: list[UploadFile] = File(...),
    ):
        owner_binding = _require_document_browser_owner(request)
        if confirmed.strip().lower() != "true":
            raise HTTPException(status_code=403, detail={"error": "owner confirmation required"})
        vault_id, vault_label = _document_source_identity(vault_id, vault_label)
        uploads = await _document_uploads(files, source_mode="obsidian")
        attachments = _document_attachments(attachment_manifest, source_mode="obsidian")
        _reject_document_path_overlap(uploads, attachments)
        digest = _document_review_digest(
            uploads=uploads, attachments=attachments, workspace=workspace,
            repo=repo, session_id=session_id, scope=scope,
            memory_type=memory_type, source_id=vault_id,
            source_label=vault_label, on_conflict=on_conflict,
            source_mode="obsidian",
        )
        _consume_document_review(
            review_token, owner_binding=owner_binding, digest=digest,
        )
        try:
            return svc.import_obsidian_upload(
                files=uploads, attachment_manifest=attachments, workspace=workspace,
                repo=repo or None, session_id=session_id or None, scope=scope,
                memory_type=memory_type, vault_id=vault_id or None,
                vault_label=vault_label, on_conflict=on_conflict, confirmed=True,
            )
        except (ValueError, KeyError):
            raise HTTPException(status_code=400, detail={"error": "invalid request"}) from None

    @app.get("/api/workspaces/import-obsidian/jobs/{job_id}", include_in_schema=False)
    def obsidian_job_alias(job_id: str, workspace: str, request: Request):
        _require_document_browser_owner(request)
        try:
            return svc.get_obsidian_import_job(job_id, workspace=workspace)
        except (ValueError, KeyError):
            raise HTTPException(status_code=404, detail={"error": "import job not found"}) from None

    @app.post("/api/workspaces/import-obsidian/jobs/{job_id}/cancel", include_in_schema=False)
    def cancel_obsidian_job_alias(job_id: str, request: Request, workspace: str = Form(...)):
        _require_document_browser_owner(request)
        try:
            return svc.cancel_obsidian_import_job(job_id, workspace=workspace)
        except (ValueError, KeyError):
            raise HTTPException(status_code=404, detail={"error": "import job not found"}) from None

    from engraphis.netutil import is_local_request

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        from engraphis.service import set_current_user

        # The open runtime has no hosted identity model. Clear any context inherited from
        # embedding applications and authorize the whole local instance as one principal.
        set_current_user(None)
        path = request.url.path
        if request.method == "OPTIONS":
            return await call_next(request)
        guarded = (
            path.startswith("/api/")
            or path == "/mcp"
            or path.startswith("/mcp/")
        )
        if not guarded or path in _PUBLIC:
            return await call_next(request)
        if (path == "/mcp" or path.startswith("/mcp/")) and not app.state.mcp_over_http:
            return JSONResponse({"error": "MCP-over-HTTP is unavailable"}, status_code=404)

        # A configured token protects every non-public API and MCP request. This is a
        # single deployment credential, not a user/seat/role authority.
        if settings.api_token:
            if bearer_ok(request.headers.get("Authorization"), settings.api_token):
                return await call_next(request)
            if browser_session_ok(
                request.cookies.get(BROWSER_SESSION_COOKIE), settings.api_token
            ):
                # Cookie authentication is for the same-origin dashboard only. Requiring
                # this non-simple header forces cross-origin callers through CORS before
                # they can exercise even side-effectful GETs such as first-use Automation.
                if request.headers.get("X-Engraphis-Browser-Session") != "1":
                    return JSONResponse({"error": "browser session header required"},
                                        status_code=403)
                return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        # Zero-config access is intentionally loopback-only. Hosted Team deployments use
        # the private cloud service, never this local app's removed account database.
        if not is_local_request(request):
            return JSONResponse(
                {
                    "error": "remote access is disabled until ENGRAPHIS_API_TOKEN is set",
                    "auth": "local-token-required",
                },
                status_code=403,
            )
        return await call_next(request)

    # New dashboard capabilities belong to the v2 application surface.  The old ``static``
    # directory remains mounted for the legacy shell and compatibility adapters only.
    if _V2_ASSETS.is_dir():
        app.mount("/v2-assets", _FreshStaticFiles(directory=str(_V2_ASSETS)), name="v2-assets")
    if _CLASSIC_ASSETS.is_dir():
        app.mount(
            "/classic-assets",
            _FreshStaticFiles(directory=str(_CLASSIC_ASSETS)),
            name="classic-assets",
        )
    if _STATIC.is_dir():
        app.mount("/static", _FreshStaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        """Serve Ledger as the production default; Classic remains at ``/classic``."""
        resp = FileResponse(_INDEX, media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    @app.get("/classic", include_in_schema=False)
    def classic_index():
        """The pre-Ledger dashboard, retained as a reversible local interface."""
        resp = FileResponse(_CLASSIC_ASSETS / "index.html", media_type="text/html")
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return resp

    for warning in licensing.production_warnings():
        import sys
        print("[engraphis] ship-safety: %s" % warning, file=sys.stderr)

    # Share the dashboard's MemoryService with the MCP server (single writer, no second
    # SQLite connection) and mount the pre-built streamable-http app at /mcp. The session
    # manager is initialized in the app's lifespan (see _lifespan above).
    if _mcp_asgi is not None:
        _mcp_mod.set_service(svc)
        app.mount("/mcp", _mcp_asgi)
        app.state.mcp_over_http = True

    # Installed LAST so it is the OUTERMOST middleware (Starlette wraps in reverse
    # registration order): the headers must also land on the 401/403/402 responses the
    # auth gate returns short of call_next, not only on successful ones.
    from engraphis import http_security
    http_security.install(app)

    return app



#: Module-level ASGI app for ``uvicorn engraphis.dashboard_app:app`` (see
#: scripts/start_dashboard.py). Built once at import.
app = create_app()
