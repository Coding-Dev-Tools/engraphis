# Engraphis — self-hosted AI memory engine. Local-first; you bring the LLM.
FROM python:3.11-slim AS base

# ENGRAPHIS_HOST is deliberately NOT set here: docker-entrypoint.sh chooses IPv6 for a
# Railway deployment (which injects RAILWAY_SERVICE_NAME) and 0.0.0.0 for ordinary Docker.
# Uvicorn's IPv6 socket is not reliably dual-stack in containers. An explicit
# ENGRAPHIS_HOST always wins over the entrypoint's default.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ENGRAPHIS_SERVICE_MODE=customer \
    ENGRAPHIS_PORT=8700 \
    ENGRAPHIS_DB_PATH=/data/engraphis.db \
    # Cache the sentence-transformers model on the persistent /data volume so it downloads
    # ONCE, not on every cold container. A fresh in-container download blocks startup and
    # can lose the healthcheck race; caching on the volume makes subsequent boots instant.
    HF_HOME=/data/.cache/huggingface \
    # Customer-side cloud session and entitlement display cache. Keep it on /data rather
    # than the container's ephemeral home so reconnects do not lose rotated credentials.
    # License issuance, trial state, leases, and revocations remain private services.
    ENGRAPHIS_STATE_DIR=/data/.engraphis

WORKDIR /app

# gosu lets the entrypoint drop from root to the non-root app user after fixing volume
# permissions (see docker-entrypoint.sh). Installed here for good layer caching.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md LICENSE NOTICE ./
COPY engraphis ./engraphis
COPY scripts ./scripts

# Railway runs CPU workloads.  Install the CPU-only PyTorch wheel before the embedding
# stack so pip cannot select PyPI's multi-gigabyte CUDA dependency chain.  The public
# customer image needs the dashboard/server surface plus its advertised local OCR path;
# MCP, transcription, PostgreSQL, and code graph remain opt-in deployment baggage.
RUN pip install --upgrade pip "setuptools>=83" \
    && pip install --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install ".[server,documents,cloud-sync]"

# Create the non-root app user and pre-own /data. NOTE: the container starts as root so
# docker-entrypoint.sh can chown a freshly-mounted (root-owned) persistent volume, then
# drops to `engraphis` via gosu — so the app still runs unprivileged at runtime.
RUN useradd --create-home --uid 10001 engraphis \
    && mkdir -p /data /data/.engraphis \
    && chown -R engraphis /data /app
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
EXPOSE 8700

# /api/ready verifies that the configured customer service can actually serve traffic;
# Railway uses the same endpoint, so a process-only health signal cannot mask a bad mode.
# start-period is generous: the first cold boot downloads the embedding model (cached to
# the /data volume via HF_HOME thereafter). The entrypoint selects a bind address suited
# to Docker or Railway; the check also honors $PORT if the platform overrides it — matching
# scripts/start_dashboard.py, which prefers $PORT over ENGRAPHIS_PORT for the bind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT') or os.environ.get('ENGRAPHIS_PORT','8700'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/ready' % p).status==200 else 1)"

# The entrypoint fixes volume ownership then drops to the non-root `engraphis` user before
# running the CMD (or any Railway/compose start-command override, which becomes its args).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default: the v2 local customer dashboard with hosted-service clients. Team identity, roles,
# seats, and organization management are hosted at the authenticated account portal, not this
# public image. This entrypoint serves /api/auth/*, /api/license/*, and /api/bootstrap.
# `--no-open`: never try to launch a browser in a container.
#
CMD ["engraphis-dashboard", "--no-open"]
