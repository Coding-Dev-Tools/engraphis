"""Launch the Engraphis server with uvicorn."""
from __future__ import annotations

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
    )


if __name__ == "__main__":
    main()
