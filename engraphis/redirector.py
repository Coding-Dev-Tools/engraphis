"""Simple redirect: port 8710 → 8700.

The Memory Inspector was merged into the main dashboard on :8700. This lightweight
server ensures old bookmarks and short-cuts pointing at :8710 still work.
"""
from __future__ import annotations
import os
import sys

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
import uvicorn


def create_app() -> FastAPI:
    app = FastAPI(title="Engraphis Redirect", docs_url=None, redoc_url=None)

    @app.get("/{path:path}", include_in_schema=False)
    async def redirect(request: Request, path: str):
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = request.headers.get("x-forwarded-host", request.headers.get("host", ""))
        host = host.partition(":")[0]
        qs = request.url.query
        target = f"{scheme}://{host}:8700/{path}"
        if qs:
            target += f"?{qs}"
        return RedirectResponse(target, status_code=301)

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("ENGRAPHIS_INSPECTOR_PORT", "8710"))
    host = os.environ.get("ENGRAPHIS_HOST", "127.0.0.1")
    print(f"Engraphis redirect — :{port} → {host}:8700")
    uvicorn.run(app, host=host, port=port, log_level="warning")
