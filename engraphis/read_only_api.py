"""Small read-only HTTP surface for shared recall and repository-graph queries."""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from engraphis.config import settings
from engraphis.inspector.auth import bearer_ok
from engraphis.service import MemoryService, ValidationError


class IntentRecallRequest(BaseModel):
    query: str
    intent: str = "recall"
    workspace: Optional[str] = None
    repo: Optional[str] = None
    mtypes: Optional[list[str]] = None
    k: int = 8
    as_of: Optional[float] = None


class CodePathRequest(BaseModel):
    workspace: str
    repo: str
    source: str
    target: str
    max_depth: int = 8


class CodeImpactRequest(BaseModel):
    workspace: str
    repo: str
    changed_files: list[str]


def create_read_only_app(service: Optional[MemoryService] = None, *,
                         token: str = "") -> FastAPI:
    svc = service or MemoryService.create(
        settings.db_path,
        embed_model=settings.embed_model or None,
        allowed_workspaces=settings.allowed_workspaces,
        extractor=settings.extractor,
    )
    expected = str(token or "")
    app = FastAPI(
        title="Engraphis Read-Only Graph API", version="1",
        docs_url=None, redoc_url=None,
    )

    @app.middleware("http")
    async def authorize(request, call_next):
        if expected and request.url.path not in {"/health", "/openapi.json"}:
            supplied = request.headers.get("authorization", "")
            if not bearer_ok(supplied, expected):
                return JSONResponse(
                    {"detail": "invalid bearer token"}, status_code=401
                )
        return await call_next(request)

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/health")
    def health():
        return {"ok": True, "mode": "read-only"}

    @app.get("/recall")
    def recall(query: str, workspace: Optional[str] = None,
               repo: Optional[str] = None, k: int = 8):
        return run(
            svc.recall, query, workspace=workspace, repo=repo, k=k,
            reinforce=False, intent="http_read_only", record_receipt=False,
        )

    @app.post("/intent/recall")
    def intent_recall(req: IntentRecallRequest):
        return run(
            svc.intent_recall, req.query, intent=req.intent,
            workspace=req.workspace, repo=req.repo, mtypes=req.mtypes,
            k=req.k, as_of=req.as_of, reinforce=False, record_receipt=False,
        )

    @app.get("/graph")
    def graph(workspace: str, limit: int = 2_000, layers: str = "",
              include_code: bool = False, repo: Optional[str] = None):
        selected = [value.strip() for value in layers.split(",") if value.strip()]
        return run(
            svc.graph, workspace=workspace, limit=limit, layers=selected or None,
            include_code=include_code, repo=repo, backfill=False,
        )

    @app.get("/code/search")
    def code_search(query: str, workspace: str, repo: str, limit: int = 20):
        return run(
            svc.search_code, query, workspace=workspace, repo=repo, limit=limit,
        )

    @app.post("/code/path")
    def code_path(req: CodePathRequest):
        return run(
            svc.code_path, req.source, req.target, workspace=req.workspace,
            repo=req.repo, max_depth=req.max_depth,
        )

    @app.post("/code/impact")
    def code_impact(req: CodeImpactRequest):
        return run(
            svc.code_impact, req.changed_files,
            workspace=req.workspace, repo=req.repo,
        )

    @app.get("/code/export")
    def code_export(workspace: str, repo: str):
        return run(svc.export_code_graph, workspace=workspace, repo=repo)

    @app.get("/receipts")
    def receipts(workspace: str, limit: int = 100):
        return run(svc.receipt_log, workspace=workspace, limit=limit)

    @app.get("/receipts/verify")
    def verify_receipts(workspace: str, expected_head: str = "",
                        expected_count: Optional[int] = None):
        return run(
            svc.verify_receipts, workspace=workspace,
            expected_head=expected_head, expected_count=expected_count,
        )

    from engraphis import http_security
    http_security.install(app)
    return app
