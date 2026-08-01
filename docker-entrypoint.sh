#!/bin/sh
# Entrypoint: make the persistent data volume writable, then drop privileges.
#
# Railway (and most managed hosts) mount a persistent volume owned by root. Engraphis
# runs as the non-root `engraphis` user (see Dockerfile), so without this the app cannot
# create /data/engraphis.db or customer state under /data/.engraphis and crashes at
# startup with `sqlite3.OperationalError: unable to open database file`.
#
# We therefore start the container as root, chown the mounted volume to `engraphis`, and
# exec the real command as `engraphis` via gosu — keeping the deliberate non-root runtime
# while making the volume writable. When not running as root (e.g. a local `docker run`
# that already dropped privileges) this is a no-op passthrough.
set -e

# Default bind host, decided at runtime (not baked into the image). Uvicorn's `::`
# listener is IPv6-only on some container kernels, so plain Docker port forwarding cannot
# reach it over IPv4. Railway injects RAILWAY_SERVICE_NAME into every deployment and needs
# IPv6 for its private-network healthchecks; ordinary Docker runs bind 0.0.0.0 instead.
# An operator-provided ENGRAPHIS_HOST always wins.
if [ -z "${ENGRAPHIS_HOST:-}" ]; then
    if [ -n "${RAILWAY_SERVICE_NAME:-}" ] && [ -f /proc/net/if_inet6 ]; then
        ENGRAPHIS_HOST="::"
    else
        ENGRAPHIS_HOST="0.0.0.0"
    fi
    export ENGRAPHIS_HOST
fi

if [ "$(id -u)" = "0" ]; then
    # ENGRAPHIS_STATE_DIR defaults to /data/.engraphis; ensure both it and the volume root
    # exist and are owned by the app user. `|| true` so a transient FS hiccup never blocks
    # startup — the app surfaces any real write failure itself.
    mkdir -p "${ENGRAPHIS_STATE_DIR:-/data/.engraphis}" 2>/dev/null || true
    chown -R engraphis:engraphis /data 2>/dev/null || true
    exec gosu engraphis "$@"
fi

exec "$@"
