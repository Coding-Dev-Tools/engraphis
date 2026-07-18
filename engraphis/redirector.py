"""Simple redirect: port 8710 → 8700.

The Memory Inspector was merged into the main dashboard on :8700. This lightweight
server ensures old bookmarks and short-cuts pointing at :8710 still work.
"""
from __future__ import annotations
import os

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse

# uvicorn is imported in __main__ only. It is the ASGI *server*, not a dependency of the
# app object, and it is absent from the [test] extra — importing it here made
# `from engraphis.redirector import create_app` raise ModuleNotFoundError under the
# documented `pip install -e ".[test]"` CI install.


def _redirect_base() -> str:
    """Canonical dashboard base URL to redirect to, taken from CONFIGURATION rather than
    the request's Host / X-Forwarded-Host headers. Reflecting those untrusted headers into
    the redirect target made this an open redirect (a spoofed header could send a victim to
    an attacker-controlled origin). ``ENGRAPHIS_DASHBOARD_URL`` wins; otherwise fall back to
    the local dashboard on :8700."""
    base = os.environ.get("ENGRAPHIS_DASHBOARD_URL", "").strip().rstrip("/")
    if base:
        return base
    from engraphis.netutil import display_base_url
    # ENGRAPHIS_HOST is a BIND host — display_base_url maps a wildcard (0.0.0.0/::) to
    # loopback and brackets IPv6, so the redirect target is always connectable.
    return display_base_url(os.environ.get("ENGRAPHIS_HOST", "127.0.0.1"), 8700)


def create_app() -> FastAPI:
    app = FastAPI(title="Engraphis Redirect", docs_url=None, redoc_url=None)
    base = _redirect_base()

    @app.get("/{path:path}", include_in_schema=False)
    async def redirect(request: Request, path: str):
        # Only the PATH and query from the request are preserved; the destination origin is
        # fixed by config, so this can't be turned into an open redirect.
        target = base + "/" + path
        qs = request.url.query
        if qs:
            target += "?" + qs
        return RedirectResponse(target, status_code=301)

    from engraphis import http_security
    http_security.install(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("ENGRAPHIS_INSPECTOR_PORT", "8710"))
    host = os.environ.get("ENGRAPHIS_HOST", "127.0.0.1")
    print(f"Engraphis redirect - :{port} -> {host}:8700")
    uvicorn.run(app, host=host, port=port, log_level="warning", proxy_headers=False)
