# Engraphis — self-hosted AI memory engine. Local-first; you bring the LLM.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ENGRAPHIS_HOST=0.0.0.0 \
    ENGRAPHIS_PORT=8700 \
    ENGRAPHIS_DB_PATH=/data/engraphis.db

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY engraphis ./engraphis
COPY scripts ./scripts

# Full stack (REST + MCP + embeddings). Drop to .[server] or .[mcp] to slim the image.
RUN pip install --upgrade pip && pip install ".[all]"

# Run as non-root and persist the database on a volume.
RUN useradd --create-home --uid 10001 engraphis && mkdir -p /data && chown -R engraphis /data /app
USER engraphis
EXPOSE 8700
# Memory Inspector (product UI): run with `docker … engraphis-inspector` or a second service.
EXPOSE 8710

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/memory/health').status==200 else 1)"

CMD ["engraphis-server"]
