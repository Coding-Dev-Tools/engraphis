"""Legacy JSON Inspector for a single local Engraphis instance.

The standalone Inspector UI was retired in favour of the unified dashboard.  This
module remains as a small, local-only inspection API for compatibility and testing.
It deliberately has no user database, sessions, roles, invitations, seats, license
issuer, analytics implementation, or automation scheduler.  Team administration and
paid compute are hosted Engraphis Cloud services.

Set ``ENGRAPHIS_API_TOKEN`` to require the same constant-time bearer check used by
the other local HTTP surfaces.  With no token, the API is intended for loopback-only
single-user use.
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from engraphis import __version__, http_security
from engraphis.config import settings
from engraphis.local_auth import bearer_ok
from engraphis.logging_setup import configure_logging
from engraphis.service import MemoryService, ValidationError

logger = logging.getLogger("engraphis")

_PUBLIC_API = {"/api/health", "/api/ready", "/api/auth/state"}


class _CorrectBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    new_content: str = Field(min_length=1, max_length=100_000)
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)


class _GovernBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)
    pinned: bool = True


class _PromoteBody(BaseModel):
    memory_id: str = Field(min_length=1, max_length=200)
    target_scope: str
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    reason: str = Field(default="", max_length=1_000)


class _ConsolidateBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    dry_run: bool = True
    min_cluster: int = Field(default=3, ge=2, le=20)
    archive_below: float = Field(default=0.05, ge=0.0, le=0.5)


def _cloud_only(feature: str) -> JSONResponse:
    return JSONResponse(
        {
            "error": f"{feature} is available only through Engraphis Cloud",
            "feature": feature,
            "cloud_only": True,
        },
        status_code=501,
    )


def create_app(
    service: Optional[MemoryService] = None,
    auth_store: Optional[object] = None,
) -> FastAPI:
    """Create the compatibility Inspector API.

    ``auth_store`` is accepted only so older embedding code fails safely during the
    open-core transition.  It is intentionally ignored: local Team/session authority
    no longer exists in the published package.
    """
    del auth_store
    configure_logging()
    app = FastAPI(title="Engraphis Memory Inspector", docs_url=None, redoc_url=None)
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins
        or ["http://127.0.0.1:8710", "http://localhost:8710"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
        allow_credentials=False,
    )

    def svc() -> MemoryService:
        if app.state.service is None:
            app.state.service = MemoryService.create(
                settings.db_path,
                embed_model=settings.embed_model or None,
                allowed_workspaces=settings.allowed_workspaces,
                extractor=settings.extractor,
            )
        return app.state.service

    @app.middleware("http")
    async def _auth_gate(request: Request, call_next):
        # A prior Team-enabled process may have left a context-local identity behind.
        # The compatibility Inspector is always single-user, so clear it explicitly.
        from engraphis.service import set_current_user

        set_current_user(None)
        path = request.url.path
        if (
            path.startswith("/api/")
            and path not in _PUBLIC_API
            and settings.api_token
            and not bearer_ok(request.headers.get("Authorization"), settings.api_token)
        ):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        del request
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        path_ref = hashlib.sha256(
            request.url.path.encode("utf-8", "replace")
        ).hexdigest()[:12]
        logger.error(
            "unhandled exception on %s path_ref=%s (%s)",
            request.method,
            path_ref,
            type(exc).__name__,
        )
        return JSONResponse({"error": "internal error -- see server logs"}, status_code=500)

    @app.get("/api/auth/state")
    async def auth_state():
        """Describe the only local auth mode; Team identity is cloud-owned."""
        mode = "token" if settings.api_token else "open"
        return JSONResponse(
            {
                "mode": mode,
                "enabled": bool(settings.api_token),
                "user": None,
                "local_multi_user": False,
                "team": {"available_locally": False, "mode": "hosted_cloud"},
            },
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.api_route(
        "/api/auth/{operation:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def hosted_team(operation: str):
        del operation
        return _cloud_only("team")

    @app.api_route("/api/license", methods=["GET", "POST"])
    @app.api_route("/api/license/{operation:path}", methods=["GET", "POST"])
    async def hosted_license(operation: str = ""):
        del operation
        return _cloud_only("license")

    @app.api_route("/api/analytics", methods=["GET", "POST"])
    @app.api_route("/api/analytics/{operation:path}", methods=["GET", "POST"])
    async def hosted_analytics(operation: str = ""):
        del operation
        return _cloud_only("analytics")

    @app.api_route("/api/automation", methods=["GET", "POST"])
    @app.api_route("/api/automation/{operation:path}", methods=["GET", "POST"])
    async def hosted_automation(operation: str = ""):
        del operation
        return _cloud_only("automation")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "engraphis-inspector"}

    @app.get("/api/ready")
    async def ready():
        checks = {"db": False, "embedder": False}
        try:
            local_service = svc()
            local_service.store.conn.execute("SELECT 1").fetchone()
            checks["db"] = True
            checks["embedder"] = getattr(local_service.engine, "embedder", None) is not None
        except Exception:
            pass
        is_ready = all(checks.values())
        return JSONResponse(
            {"ready": is_ready, "checks": checks, "version": __version__},
            status_code=200 if is_ready else 503,
        )

    @app.get("/api/workspaces")
    async def workspaces():
        return svc().list_workspaces()

    @app.get("/api/stats")
    async def stats(workspace: Optional[str] = None):
        return svc().stats(workspace=workspace)

    @app.get("/api/recall")
    async def recall(q: str, workspace: str, repo: Optional[str] = None, k: int = 12):
        return svc().recall(q, workspace=workspace, repo=repo, k=k, reinforce=False)

    @app.get("/api/why")
    async def why(q: str, workspace: str, repo: Optional[str] = None, k: int = 5):
        return svc().why(q, workspace=workspace, repo=repo, k=k)

    @app.get("/api/timeline")
    async def timeline(
        q: str, workspace: str, repo: Optional[str] = None, limit: int = 20
    ):
        return svc().timeline(q, workspace=workspace, repo=repo, limit=limit)

    @app.get("/api/proactive")
    async def proactive(workspace: str, repo: Optional[str] = None, k: int = 10):
        return svc().recall_proactive(workspace=workspace, repo=repo, k=k)

    @app.get("/api/memory/{memory_id}")
    async def memory(memory_id: str, workspace: str, repo: Optional[str] = None):
        return svc().inspect(memory_id, workspace=workspace, repo=repo)

    @app.get("/api/audit")
    async def audit_log(workspace: str, limit: int = 100):
        return svc().audit_log(workspace=workspace, limit=limit)

    @app.get("/api/receipts")
    async def receipts(workspace: str, limit: int = 100):
        return svc().receipt_log(workspace=workspace, limit=limit)

    @app.get("/api/receipts/verify")
    async def receipts_verify(workspace: str):
        return svc().verify_receipts(workspace=workspace)

    @app.get("/api/graph")
    async def graph(
        workspace: str,
        limit: int = 2000,
        layers: Optional[str] = None,
        include_code: bool = False,
        repo: Optional[str] = None,
    ):
        selected = (
            None
            if layers is None
            else [item.strip() for item in layers.split(",") if item.strip()]
        )
        return svc().graph(
            workspace=workspace,
            limit=limit,
            layers=selected,
            include_code=include_code,
            repo=repo,
            backfill=False,
        )

    @app.get("/api/export")
    async def export(workspace: str):
        # Local data portability is not a paid algorithm.  The compatibility API has
        # already applied its optional bearer boundary, so bypass the retired local
        # entitlement gate and let the owner recover their complete workspace.
        data = svc().export_workspace(workspace=workspace, recovery=True)
        filename = "engraphis-export-%s-%s.json" % (
            workspace.replace("/", "_"),
            time.strftime("%Y%m%d"),
        )
        return JSONResponse(
            data,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/pin")
    async def pin(body: _GovernBody):
        return svc().pin(
            body.memory_id,
            workspace=body.workspace,
            repo=body.repo,
            pinned=body.pinned,
            actor="inspector-local",
        )

    @app.post("/api/forget")
    async def forget(body: _GovernBody):
        return svc().forget(
            body.memory_id,
            workspace=body.workspace,
            repo=body.repo,
            reason=body.reason,
            actor="inspector-local",
        )

    @app.post("/api/correct")
    async def correct(body: _CorrectBody):
        return svc().correct(
            body.memory_id,
            body.new_content,
            workspace=body.workspace,
            repo=body.repo,
            reason=body.reason,
            actor="inspector-local",
        )

    @app.post("/api/promote")
    async def promote(body: _PromoteBody):
        return svc().promote(
            body.memory_id,
            body.target_scope,
            workspace=body.workspace,
            repo=body.repo,
            reason=body.reason,
            actor="inspector-local",
        )

    @app.post("/api/consolidate")
    async def consolidate(body: _ConsolidateBody):
        # This is an explicit manual sweep. Scheduling, dreaming/inference, and
        # automatic consolidation belong to the hosted automation worker.
        return svc().consolidate(
            workspace=body.workspace,
            repo=body.repo,
            dry_run=body.dry_run,
            min_cluster=body.min_cluster,
            archive_below=body.archive_below,
            infer=False,
        )

    http_security.install(app)
    return app
