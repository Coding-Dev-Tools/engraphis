"""Inspector HTTP layer — a thin FastAPI binding over :class:`MemoryService`.

Deliberately mirrors ``mcp_server.py``'s philosophy: no logic here, only transport.
All validation/authorization lives in the service (workspace binding included), so
the inspector inherits the same isolation guarantees as the MCP tools. Optional
bearer-token auth via ``ENGRAPHIS_API_TOKEN`` (same knob as the v1 server); CORS is
loopback-only by default. Responses are JSON; the single HTML page renders
everything client-side with ``textContent`` (no innerHTML on stored content — the
stored-XSS lesson from the v1 dashboard, applied from day one).
"""
from __future__ import annotations

import hmac
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from engraphis.config import settings
from engraphis.service import MemoryService, ValidationError

_INDEX = Path(__file__).parent / "index.html"


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


class _ConsolidateBody(BaseModel):
    workspace: str = Field(min_length=1, max_length=200)
    repo: Optional[str] = Field(default=None, max_length=200)
    dry_run: bool = True
    min_cluster: int = Field(default=3, ge=2, le=20)
    archive_below: float = Field(default=0.05, ge=0.0, le=0.5)


def create_app(service: Optional[MemoryService] = None) -> FastAPI:
    app = FastAPI(title="Engraphis Memory Inspector", docs_url=None, redoc_url=None)
    app.state.service = service

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins or ["http://127.0.0.1:8710",
                                                "http://localhost:8710"],
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
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
    async def _bearer_auth(request: Request, call_next):
        token = settings.api_token
        if token and request.url.path.startswith("/api/"):
            supplied = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied, token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ValidationError)
    async def _validation(request: Request, exc: ValidationError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    # ── page ────────────────────────────────────────────────────────────────
    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(_INDEX, media_type="text/html")

    # ── read ────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    async def health():
        return {"status": "ok", "service": "engraphis-inspector"}

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
    async def timeline(q: str, workspace: str, repo: Optional[str] = None, limit: int = 20):
        return svc().timeline(q, workspace=workspace, repo=repo, limit=limit)

    @app.get("/api/proactive")
    async def proactive(workspace: str, repo: Optional[str] = None, k: int = 10):
        return svc().recall_proactive(workspace=workspace, repo=repo, k=k)

    @app.get("/api/memory/{memory_id}")
    async def memory(memory_id: str, workspace: str, repo: Optional[str] = None):
        return svc().inspect(memory_id, workspace=workspace, repo=repo)

    @app.get("/api/audit")
    async def audit(workspace: str, limit: int = 100):
        return svc().audit_log(workspace=workspace, limit=limit)

    # ── governance (audited; never a hard delete) ───────────────────────────
    @app.post("/api/pin")
    async def pin(body: _GovernBody):
        return svc().pin(body.memory_id, workspace=body.workspace, repo=body.repo,
                         pinned=body.pinned)

    @app.post("/api/forget")
    async def forget(body: _GovernBody):
        return svc().forget(body.memory_id, workspace=body.workspace, repo=body.repo,
                            reason=body.reason)

    @app.post("/api/correct")
    async def correct(body: _CorrectBody):
        return svc().correct(body.memory_id, body.new_content, workspace=body.workspace,
                             repo=body.repo, reason=body.reason)

    @app.post("/api/consolidate")
    async def consolidate(body: _ConsolidateBody):
        return svc().consolidate(workspace=body.workspace, repo=body.repo,
                                 dry_run=body.dry_run, min_cluster=body.min_cluster,
                                 archive_below=body.archive_below)

    return app
