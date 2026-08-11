"""Small read-only HTTP surface for shared recall and repository-graph queries."""
from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
import json
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt

from engraphis.config import settings
from engraphis.local_auth import bearer_ok
from engraphis.netutil import is_local_request
from engraphis.service import (
    DEFAULT_CODE_QUERY_CAPACITY,
    MAX_CODE_QUERY_CAPACITY,
    MemoryService,
    ValidationError,
)


logger = logging.getLogger("engraphis.read_only")
MAX_READ_ONLY_BODY_BYTES = 2_000_000
MAX_READ_ONLY_TEXT_CHARS = 100_000
MAX_READ_ONLY_LIST_ITEMS = 2_000


class BodyTooLarge(Exception):
    """Internal marker: a streamed request exceeded the body limit.

    Raised inside the receive hook where FastAPI's request-body parser would
    otherwise swallow it as a generic parse error; the middleware translates
    it to the same 413 the declared-length path returns.
    """


class IntentRecallRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_READ_ONLY_TEXT_CHARS)
    intent: str = Field("recall", max_length=64)
    workspace: Optional[str] = Field(None, max_length=256)
    repo: Optional[str] = Field(None, max_length=256)
    mtypes: Optional[list[str]] = Field(None, max_length=16)
    k: int = Field(8, ge=1, le=500)
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None
    token_budget: Optional[int] = Field(None, ge=1, le=100_000)
    retrieval_profile: str = Field("balanced", max_length=32)
    candidate_depth: str = Field("fixed", max_length=32)
    response_mode: str = Field("compact", max_length=32)
    diagnostics: bool = False
    planning: str = Field("off", max_length=32)
    mtype_limits: Optional[dict[str, StrictInt]] = Field(None, max_length=16)


class CodePathRequest(BaseModel):
    workspace: str = Field(..., min_length=1, max_length=256)
    repo: str = Field(..., min_length=1, max_length=256)
    source: str = Field(..., min_length=1, max_length=MAX_READ_ONLY_TEXT_CHARS)
    target: str = Field(..., min_length=1, max_length=MAX_READ_ONLY_TEXT_CHARS)
    max_depth: int = Field(8, ge=1, le=128)
    capacity: int = Field(
        default=DEFAULT_CODE_QUERY_CAPACITY, ge=1, le=MAX_CODE_QUERY_CAPACITY
    )
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None


class CodeImpactRequest(BaseModel):
    workspace: str = Field(..., min_length=1, max_length=256)
    repo: str = Field(..., min_length=1, max_length=256)
    changed_files: list[str] = Field(
        ..., min_length=1, max_length=MAX_READ_ONLY_LIST_ITEMS,
    )
    capacity: int = Field(
        default=DEFAULT_CODE_QUERY_CAPACITY, ge=1, le=MAX_CODE_QUERY_CAPACITY
    )
    as_of: Optional[float] = None
    valid_at: Optional[float] = None
    known_at: Optional[float] = None


def create_read_only_app(service: Optional[MemoryService] = None, *,
                         token: str = "") -> FastAPI:
    owns_service = service is None
    svc = service or MemoryService.create(
        settings.db_path,
        embed_model=settings.embed_model or None,
        embed_revision=getattr(settings, "embed_revision", "") or None,
        require_immutable_models=bool(getattr(settings, "require_immutable_models", False)),
        embed_dim=settings.embed_dim if settings.embed_dim is not None else 384,
        allowed_workspaces=settings.allowed_workspaces,
        vector_backend=settings.vector_backend,
        rerank_model=getattr(settings, "rerank_model", "") or None,
        rerank_revision=getattr(settings, "rerank_revision", "") or None,
        extractor=settings.extractor,
        read_only=True,
    )

    @asynccontextmanager
    async def _owned_service_lifespan(_app: FastAPI):
        try:
            yield
        finally:
            if owns_service:
                await asyncio.to_thread(svc.close)

    expected = str(token or "")
    app = FastAPI(
        title="Engraphis Read-Only Graph API", version="1",
        docs_url=None, redoc_url=None, lifespan=_owned_service_lifespan,
    )
    app.state.service = svc
    app.state.owns_service = owns_service

    @app.middleware("http")
    async def authorize(request, call_next):
        public = request.url.path in {"/health", "/openapi.json"}
        if expected and not public:
            if not bearer_ok(request.headers.get("authorization", ""), expected):
                return JSONResponse(
                    {"detail": "invalid bearer token"}, status_code=401
                )
        elif not expected and not public and not is_local_request(request):
            # The packaged launcher refuses a tokenless non-loopback bind, but keep the
            # same boundary inside the ASGI factory too. This prevents a direct
            # ``uvicorn ... --factory --host 0.0.0.0`` invocation (or an embedding app)
            # from publishing workspace content merely by bypassing the launcher.
            return JSONResponse(
                {"detail": "remote access requires a bearer token"}, status_code=403
            )
        return await call_next(request)

    @app.middleware("http")
    async def redact_unhandled_errors(request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001 - public HTTP error boundary
            path_ref = hashlib.sha256(
                request.url.path.encode("utf-8", "replace")
            ).hexdigest()[:12]
            logger.error(
                "read-only request failed path=%s (%s)",
                path_ref,
                type(exc).__name__,
            )
            return JSONResponse(
                {"error": "internal server error"}, status_code=500
            )

    @app.middleware("http")
    async def limit_request_body(request, call_next):
        content_length = request.headers.get("content-length")
        try:
            declared_length = int(content_length) if content_length else 0
        except ValueError:
            declared_length = 0
        if declared_length > MAX_READ_ONLY_BODY_BYTES:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        received = 0
        original_receive = request.receive

        async def limited_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body") or b"")
                if received > MAX_READ_ONLY_BODY_BYTES:
                    # Raising here is consumed by FastAPI's request-body parser for
                    # chunked/streamed bodies, which reports a generic 400 before our
                    # middleware can translate it. Emit the 413 response directly.
                    raise BodyTooLarge
            return message

        request._receive = limited_receive
        try:
            return await call_next(request)
        except BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except ValueError as exc:
            if str(exc) == "request body too large":
                return JSONResponse(
                    {"detail": "request body too large"}, status_code=413
                )
            raise

    def run(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/health")
    def health():
        return {"ok": True, "mode": "read-only"}

    @app.get("/recall")
    def recall(query: str = Query(..., min_length=1, max_length=MAX_READ_ONLY_TEXT_CHARS),
               workspace: Optional[str] = Query(None, max_length=256),
               repo: Optional[str] = Query(None, max_length=256),
               k: int = Query(8, ge=1, le=500),
               as_of: Optional[float] = None,
               valid_at: Optional[float] = None,
               known_at: Optional[float] = None,
               token_budget: Optional[int] = Query(None, ge=1, le=100_000),
               retrieval_profile: str = Query("balanced", max_length=32),
               candidate_depth: str = Query("fixed", max_length=32),
               response_mode: str = Query("compact", max_length=32),
               diagnostics: bool = False,
               planning: str = Query("off", max_length=32),
               mtype_limits: Optional[str] = Query(None, max_length=4_000)):
        try:
            parsed_limits = json.loads(mtype_limits) if mtype_limits else None
            if parsed_limits is not None and not isinstance(parsed_limits, dict):
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid mtype_limits") from exc
        return run(
            svc.recall, query, workspace=workspace, repo=repo, k=k,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
            token_budget=token_budget, retrieval_profile=retrieval_profile,
            candidate_depth=candidate_depth,
            response_mode=response_mode, diagnostics=diagnostics,
            planning=planning, mtype_limits=parsed_limits,
            reinforce=False, intent="http_read_only", record_receipt=False,
        )

    @app.post("/intent/recall")
    def intent_recall(req: IntentRecallRequest):
        return run(
            svc.intent_recall, req.query, intent=req.intent,
            workspace=req.workspace, repo=req.repo, mtypes=req.mtypes,
            k=req.k, as_of=req.as_of, valid_at=req.valid_at,
            known_at=req.known_at, token_budget=req.token_budget,
            retrieval_profile=req.retrieval_profile,
            candidate_depth=req.candidate_depth,
            response_mode=req.response_mode, diagnostics=req.diagnostics,
            planning=req.planning, mtype_limits=req.mtype_limits,
            reinforce=False, record_receipt=False,
        )

    @app.get("/graph")
    def graph(workspace: str, limit: int = Query(default=2_000, ge=1, le=5_000),
              include_code: bool = False, repo: Optional[str] = None,
              as_of: Optional[float] = None,
              valid_at: Optional[float] = None,
              known_at: Optional[float] = None):
        selected = None if layers is None else [
            value.strip() for value in layers.split(",") if value.strip()
        ]
        return run(
            svc.graph, workspace=workspace, limit=limit, layers=selected,
            include_code=include_code, repo=repo, backfill=False,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )

    @app.get("/code/search")
    def code_search(query: str, workspace: str, repo: str, limit: int = Query(default=20, ge=1, le=1_000),
                    as_of: Optional[float] = None,
                    valid_at: Optional[float] = None,
                    known_at: Optional[float] = None):
        return run(
            svc.search_code, query, workspace=workspace, repo=repo, limit=limit,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )

    @app.post("/code/path")
    def code_path(req: CodePathRequest):
        return run(
            svc.code_path, req.source, req.target, workspace=req.workspace,
            repo=req.repo, max_depth=req.max_depth, capacity=req.capacity,
            as_of=req.as_of, valid_at=req.valid_at, known_at=req.known_at,
        )

    @app.post("/code/impact")
    def code_impact(req: CodeImpactRequest):
        return run(
            svc.code_impact, req.changed_files,
            workspace=req.workspace, repo=req.repo, capacity=req.capacity,
            as_of=req.as_of, valid_at=req.valid_at, known_at=req.known_at,
        )

    @app.get("/code/export")
    def code_export(workspace: str, repo: str,
                    capacity: int = Query(
                        default=DEFAULT_CODE_QUERY_CAPACITY,
                        ge=1,
                        le=MAX_CODE_QUERY_CAPACITY,
                    ),
                    as_of: Optional[float] = None,
                    valid_at: Optional[float] = None,
                    known_at: Optional[float] = None):
        return run(
            svc.export_code_graph, workspace=workspace, repo=repo, capacity=capacity,
            as_of=as_of, valid_at=valid_at, known_at=known_at,
        )

    @app.get("/receipts")
    def receipts(workspace: str, limit: int = Query(default=100, ge=1, le=10_000)):
        return run(svc.receipt_log, workspace=workspace, limit=limit)

    @app.get("/context-savings")
    def context_savings(
        workspace: Optional[str] = None,
        repo: Optional[str] = None,
        from_ts: Optional[float] = None,
        to_ts: Optional[float] = None,
        release_version: Optional[str] = None,
        format: Optional[str] = None,
        group_by: Optional[str] = None,
    ):
        return run(
            svc.context_savings,
            workspace=workspace.strip() if isinstance(workspace, str) else None,
            repo=repo,
            from_ts=from_ts,
            to_ts=to_ts,
            release_version=release_version,
            format=format,
            group_by=group_by,
        )

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
