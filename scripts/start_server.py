"""Launch the Engraphis server with uvicorn."""
from __future__ import annotations

import os
import sys

import uvicorn

from engraphis.config import settings


def main() -> None:
    print(f"Engraphis — starting on {settings.host}:{settings.port}")
    print(f"  Database:     {settings.db_path}")
    print(f"  Embed model:  {settings.embed_model}")
    print(f"  LLM provider: {settings.llm_provider} / {settings.llm_model}")
    print(f"  Loop interval: {settings.loop_interval}s")
    print(f"  SDK base URL: {settings.base_url}")
    print(f"  Docs:         {settings.base_url}/docs")
    print()
    uvicorn.run(
        "engraphis.app:app",
        host=settings.host,
        port=settings.port,
        reload="--reload" in sys.argv,
        # Honor X-Forwarded-Proto/-For from the fronting TLS proxy (Railway/Fly/nginx)
        # so request.url.scheme is "https" and the session cookie's Secure flag is set.
        # Default "127.0.0.1" trusts NO forwarded headers (safe when the port is published
        # directly). Set ENGRAPHIS_FORWARDED_ALLOW_IPS to the proxy's IP/CIDR (or "*" if the
        # container is reachable ONLY via that trusted proxy) to enable https detection.
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("ENGRAPHIS_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )


if __name__ == "__main__":
    main()
