# Engraphis — self-hosted AI memory engine. Local-first; you bring the LLM.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ENGRAPHIS_HOST=0.0.0.0 \
    ENGRAPHIS_PORT=8700 \
    ENGRAPHIS_DB_PATH=/data/engraphis.db \
    # License / trial / machine-id / lease / relay-registry state. Kept on the /data
    # volume (not the container's ephemeral home) so activated keys, the one-time trial,
    # device binding, and — critically — the revocation registry survive redeploys.
    ENGRAPHIS_STATE_DIR=/data/.engraphis

WORKDIR /app

# gosu lets the entrypoint drop from root to the non-root app user after fixing volume
# permissions (see docker-entrypoint.sh). Installed here for good layer caching.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY engraphis ./engraphis
COPY scripts ./scripts

# Full stack (REST + MCP + embeddings). Drop to .[server] or .[mcp] to slim the image.
RUN pip install --upgrade pip && pip install ".[all]"

# Create the non-root app user and pre-own /data. NOTE: the container starts as root so
# docker-entrypoint.sh can chown a freshly-mounted (root-owned) persistent volume, then
# drops to `engraphis` via gosu — so the app still runs unprivileged at runtime.
RUN useradd --create-home --uid 10001 engraphis \
    && mkdir -p /data /data/.engraphis \
    && chown -R engraphis /data /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
EXPOSE 8700

# /api/health is served by BOTH entrypoints (engraphis-server AND engraphis-dashboard),
# so this check is correct regardless of which one a service runs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8700/api/health').status==200 else 1)"

# The entrypoint fixes volume ownership then drops to the non-root `engraphis` user before
# running the CMD (or any Railway/compose start-command override, which becomes its args).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default: the raw v1 API server (single-user). For a multi-user TEAM deployment run the
# dashboard instead — `engraphis-dashboard --no-open` — which serves team auth, roles,
# seats, cloud-license revocation and Pro sync. docker-compose.yml does this by default.
CMD ["engraphis-server"]
