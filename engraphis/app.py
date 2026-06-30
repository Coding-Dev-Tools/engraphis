"""FastAPI app assembly — mounts all routes, serves dashboard, initializes DB, starts background loop."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from engraphis.config import settings
from engraphis.engines import reweight, thoughts as thoughts_engine
from engraphis.routes.memory import router as memory_router
from engraphis.routes.vault import router as vault_router
from engraphis.stores import init_db

logger = logging.getLogger("engraphis")

_background_task: asyncio.Task | None = None
_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    app = FastAPI(
        title="Engraphis",
        description="Self-hosted AI memory system — Ebbinghaus decay, interaction-aware "
                    "recall, conscious thought synthesis. Drop-in replacement for the "
                    "Engraphis cloud API.",
        version="0.1.0",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    init_db()
    app.include_router(memory_router)
    app.include_router(vault_router)

    @app.on_event("startup")
    async def _startup() -> None:
        global _background_task
        if settings.loop_interval > 0:
            _background_task = asyncio.create_task(_consciousness_loop())
            logger.info("Background consciousness loop started (interval=%ds)", settings.loop_interval)
        else:
            logger.info("Background loop disabled (ENGRAPHIS_LOOP_INTERVAL=0)")

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        global _background_task
        if _background_task:
            _background_task.cancel()
            try:
                await _background_task
            except asyncio.CancelledError:
                pass

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        """Serve the visual dashboard."""
        index_path = _STATIC_DIR / "index.html"
        if index_path.exists():
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        return HTMLResponse("<h1>Dashboard not found</h1><p>Static files missing at: "
                            f"{_STATIC_DIR}</p>", status_code=404)

    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    return app


async def _consciousness_loop() -> None:
    """Phase 2 + Phase 4 background cycle: decay → thought synthesis → reweight."""
    while True:
        try:
            await asyncio.sleep(settings.loop_interval)
            touched = reweight.decay_pass(namespace=None)
            if touched:
                logger.info("Decay pass: %d memories reweighted", touched)
            result = thoughts_engine.synthesize_thoughts(
                namespace=None,
                max_chunks=settings.loop_top_k,
                persist=True,
            )
            if result.get("persisted"):
                logger.info("Thought synthesized: %s", result.get("thought"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Consciousness loop error: %s", e)


app = create_app()
