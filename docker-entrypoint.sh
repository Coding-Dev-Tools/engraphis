#!/bin/sh
# Entrypoint: make the persistent data volume writable, then drop privileges.
#
# Railway (and most managed hosts) mount a persistent volume owned by root. Engraphis
# runs as the non-root `engraphis` user (see Dockerfile), so without this the app cannot
# create /data/engraphis.db or /data/.engraphis/relay.db on the volume and crashes at
# startup with `sqlite3.OperationalError: unable to open database file`.
#
# We therefore start the container as root, chown the mounted volume to `engraphis`, and
# exec the real command as `engraphis` via gosu — keeping the deliberate non-root runtime
# while making the volume writable. When not running as root (e.g. a local `docker run`
# that already dropped privileges) this is a no-op passthrough.
set -e

if [ "$(id -u)" = "0" ]; then
    # ENGRAPHIS_STATE_DIR defaults to /data/.engraphis; ensure both it and the volume root
    # exist and are owned by the app user. `|| true` so a transient FS hiccup never blocks
    # startup — the app surfaces any real write failure itself.
    mkdir -p "${ENGRAPHIS_STATE_DIR:-/data/.engraphis}" 2>/dev/null || true
    chown -R engraphis:engraphis /data 2>/dev/null || true
    exec gosu engraphis "$@"
fi

exec "$@"
