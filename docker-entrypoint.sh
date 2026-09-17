#!/bin/sh
# Entrypoint: make the persistent data volume writable, then drop privileges.
#
# Railway (and most managed hosts) mount a persistent volume owned by root. Engraphis
# runs as the non-root `engraphis` user (see Dockerfile), so without this the app cannot
# create /data/engraphis.db or customer state under /data/.engraphis and crashes at
# startup with `sqlite3.OperationalError: unable to open database file`.
#
# We therefore start the container as root, repair ownership once, and exec the real command
# as `engraphis` via gosu — keeping the deliberate non-root runtime while making the volume
# writable. A marker avoids recursively walking a large Hugging Face cache on every restart.
# When not running as root (e.g. a local `docker run` that already dropped privileges) this is
# a no-op passthrough.
set -e

# Refuse any symlink in a path before a root-owned mkdir/chmod/chown can follow
# it. Checking only the leaf is insufficient when an app-writable parent can
# be swapped for a link between container restarts.
reject_symlink_components() {
    candidate=$1
    while [ -n "$candidate" ] && [ "$candidate" != "/" ] && [ "$candidate" != "." ]; do
        if [ -L "$candidate" ]; then
            printf '%s\n' "[engraphis] refusing symlinked path component: $candidate" >&2
            return 1
        fi
        parent=$(dirname "$candidate")
        if [ "$parent" = "$candidate" ]; then
            break
        fi
        candidate=$parent
    done
    return 0
}

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
    # ENGRAPHIS_STATE_DIR defaults to /data/.engraphis. Repair the complete volume only on
    # first boot; later restarts verify the mount and state roots without walking the cache.
    state_dir="${ENGRAPHIS_STATE_DIR:-/data/.engraphis}"
    ownership_marker="${state_dir}/.volume-ownership"
    config_file="${ENGRAPHIS_ENV_FILE:-}"
    # These paths are trusted root-owned state locations.  Check them before
    # mkdir/chown so an app-controlled symlink cannot redirect root ownership
    # repair to an unrelated file or directory.
    if [ -L "$state_dir" ] || [ -L "$ownership_marker" ] \
        || ! reject_symlink_components "$state_dir" \
        || ! reject_symlink_components "$ownership_marker"; then
        printf '%s\n' "[engraphis] refusing symlinked state path: $state_dir" >&2
        exit 1
    fi
    if ! mkdir -p "$state_dir"; then
        printf '%s\n' "[engraphis] unable to create state directory: $state_dir" >&2
        exit 1
    fi
    if [ -n "$config_file" ]; then
        config_parent=$(dirname "$config_file")
        if ! reject_symlink_components "$config_parent"; then
            printf '%s\n' "[engraphis] refusing symlinked trusted config parent: $config_parent" >&2
            exit 1
        fi
        if ! mkdir -p "$config_parent"; then
            printf '%s\n' "[engraphis] unable to create config directory: $config_parent" >&2
            exit 1
        fi
        if ! reject_symlink_components "$config_parent" || [ -L "$config_file" ]; then
            printf '%s\n' "[engraphis] refusing symlinked trusted config file: $config_file" >&2
            exit 1
        fi
        if [ ! -e "$config_file" ] && ! : > "$config_file"; then
            printf '%s\n' "[engraphis] unable to create trusted config file: $config_file" >&2
            exit 1
        fi
        if ! chmod 600 "$config_file"; then
            printf '%s\n' "[engraphis] unable to restrict trusted config file: $config_file" >&2
            exit 1
        fi
        if ! reject_symlink_components "$config_parent" || [ -L "$config_file" ]; then
            printf '%s\n' "[engraphis] refusing symlinked trusted config file: $config_file" >&2
            exit 1
        fi
    fi
    if [ -L "$state_dir" ] || [ -L "$ownership_marker" ] \
        || ! reject_symlink_components "$state_dir" \
        || ! reject_symlink_components "$ownership_marker"; then
        printf '%s\n' "[engraphis] refusing symlinked state path: $state_dir" >&2
        exit 1
    fi
    if [ ! -e "$ownership_marker" ]; then
        if ! chown -R engraphis:engraphis /data; then
            printf '%s\n' "[engraphis] unable to repair /data ownership" >&2
            exit 1
        fi
        if ! : > "$ownership_marker"; then
            printf '%s\n' "[engraphis] unable to create volume ownership marker" >&2
            exit 1
        fi
        if ! chown engraphis:engraphis "$ownership_marker"; then
            printf '%s\n' "[engraphis] unable to own volume ownership marker" >&2
            exit 1
        fi
    elif ! chown engraphis:engraphis /data "$state_dir" "$ownership_marker"; then
        printf '%s\n' "[engraphis] unable to verify /data ownership" >&2
        exit 1
    fi
    if [ -n "$config_file" ] && ! chown engraphis:engraphis "$config_file"; then
        printf '%s\n' "[engraphis] unable to own trusted config file" >&2
        exit 1
    fi
    exec gosu engraphis "$@"
fi

exec "$@"
