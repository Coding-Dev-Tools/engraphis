# Engraphis — self-hosted AI memory engine. Local-first; you bring the LLM.
FROM python:3.11-slim AS base

# ENGRAPHIS_HOST is deliberately NOT set here: docker-entrypoint.sh picks the widest
# workable bind at runtime — `::` (dual-stack: IPv6 AND IPv4) when the kernel has IPv6,
# else 0.0.0.0. Railway healthchecks arrive over the IPv6 private network, so a baked-in
# IPv4-only bind is exactly what caused the 2026-07-16 deploy outage; hard-coding `::`
# instead would break rarer IPv6-disabled hosts. An explicit ENGRAPHIS_HOST env (e.g.
# docker-compose.yml's 0.0.0.0) always wins over the entrypoint's default.
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
    # License / trial / machine-id / lease / relay-registry state. Kept on the /data
    # volume (not the container's ephemeral home) so activated keys, the one-time trial,
    # device binding, and — critically — the revocation registry survive redeploys.
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

# Full stack (REST + MCP + embeddings). Drop to .[server] or .[mcp] to slim the image.
RUN pip install --upgrade pip "setuptools>=83" && pip install ".[all]"

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
# the /data volume via HF_HOME thereafter). The entrypoint's default bind (`::` dual-stack
# where available, else 0.0.0.0) answers loopback IPv4 probes like this one AND platform
# IPv6 routing; the check also honors $PORT if the platform overrides it — matching
# scripts/start_dashboard.py, which prefers $PORT over ENGRAPHIS_PORT for the bind.
HEALTHCHECK --interval=30s --timeout=5s --start-period=300s --retries=3 \
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT') or os.environ.get('ENGRAPHIS_PORT','8700'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/ready' % p).status==200 else 1)"

# The entrypoint fixes volume ownership then drops to the non-root `engraphis` user before
# running the CMD (or any Railway/compose start-command override, which becomes its args).
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]

# Default: the v2 team dashboard (multi-user auth, roles, seats, cloud-license
# revocation, Pro sync) — this is what docker-compose.yml already defaults to, and what
# every hosted deployment (e.g. Railway) needs, since it's the only entrypoint that
# serves /api/auth/*, /api/license/*, and /api/bootstrap. `--no-open`: never try to launch
# a browser in a container.
#
# The raw v1 single-user API server is still available — run `engraphis-server` directly
# (see docker-compose.yml's opt-in "api" profile) — but it shares this image's exposed
# port and root route (it serves the same static/index.html) while answering every
# /api/* call with a blanket bearer-token 401, INCLUDING /api/auth/state and
# /api/license/*. If this image is ever run as `engraphis-server` behind a team-mode
# frontend, the UI will render normally but look permanently "signed out with no
# features" no matter what the user does — that exact symptom cost real prod downtime
# on 2026-07-13 when a host's start command silently fell back to this default. Do not
# revert this without also fixing that ambiguity.
CMD ["engraphis-dashboard", "--no-open"]
