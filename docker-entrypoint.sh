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
    # Validate every existing component without resolving through a symlink. The trusted
    # config path is operator-configured and may be outside /data, so checking only its
    # leaf or final parent would let an app-writable intermediate directory redirect root's
    # chmod/chown into the image. Reject dot-dot paths rather than guessing their target.
    reject_linked_path() {
        path=$1
        case "$path" in
            /*) ;;
            *) return 1 ;;
        esac
        remainder=${path#/}
        current=
        while [ -n "$remainder" ]; do
            case "$remainder" in
                */*)
                    component=${remainder%%/*}
                    remainder=${remainder#*/}
                    ;;
                *)
                    component=$remainder
                    remainder=
                    ;;
            esac
            case "$component" in
                ""|.) continue ;;
                ..) return 1 ;;
            esac
            if [ -n "$current" ]; then
                current="$current/$component"
            else
                current="/$component"
            fi
            if [ -L "$current" ]; then
                return 1
            fi
        done
        return 0
    }

    state_directory_is_owned() {
        # Only /data is an ownership-repair target. External state paths must be
        # provisioned for the app beforehand, including on the first boot.
        case "$1" in
            /data|/data/*) return 0 ;;
        esac
        [ -d "$1" ] && [ "$(stat -c '%u' "$1" 2>/dev/null)" = "$2" ]
    }

    # ENGRAPHIS_STATE_DIR defaults to /data/.engraphis. Repair the complete volume only on
    # first boot; later restarts verify the mount and state roots without walking the cache.
    state_dir="${ENGRAPHIS_STATE_DIR:-/data/.engraphis}"
    # Keep the marker on the volume it describes; external state may outlive a
    # replaced /data volume that still needs its first ownership repair.
    ownership_marker="/data/.volume-ownership"
    config_file="${ENGRAPHIS_ENV_FILE:-}"
    app_owner=$(id -u engraphis)
    if ! reject_linked_path "$state_dir"; then
        printf '%s\n' "[engraphis] refusing linked or unnormalized state path: $state_dir" >&2
        exit 1
    fi
    # The state directory is app-writable after first boot. Reject a planted link or
    # non-directory before mkdir/chown can follow it into a root-owned image path.
    if [ -L "$state_dir" ]; then
        printf '%s\n' "[engraphis] refusing symlinked state directory: $state_dir" >&2
        exit 1
    elif [ -e "$state_dir" ] && [ ! -d "$state_dir" ]; then
        printf '%s\n' "[engraphis] refusing non-directory state path: $state_dir" >&2
        exit 1
    fi
    if ! state_directory_is_owned "$state_dir" "$app_owner"; then
        printf '%s\n' "[engraphis] external state directory must already be owned by engraphis: $state_dir" >&2
        exit 1
    fi
    if ! mkdir -p "$state_dir"; then
        printf '%s\n' "[engraphis] unable to create state directory: $state_dir" >&2
        exit 1
    fi
    if [ -n "$config_file" ]; then
        if ! reject_linked_path "$config_file"; then
            printf '%s\n' "[engraphis] refusing linked or unnormalized trusted config path: $config_file" >&2
            exit 1
        fi
        config_parent=$(dirname "$config_file")
        if [ -L "$config_parent" ]; then
            printf '%s\n' "[engraphis] refusing symlinked trusted config directory: $config_parent" >&2
            exit 1
        fi
        config_parent_created=0
        if [ ! -e "$config_parent" ]; then
            config_parent_created=1
        elif [ ! -d "$config_parent" ]; then
            printf '%s\n' "[engraphis] refusing non-directory trusted config parent: $config_parent" >&2
            exit 1
        fi
        if ! mkdir -p "$config_parent"; then
            printf '%s\n' "[engraphis] unable to create config directory: $config_parent" >&2
            exit 1
        fi
        if [ "$config_parent_created" = "1" ]; then
            if ! reject_linked_path "$config_parent" || [ ! -d "$config_parent" ]; then
                printf '%s\n' "[engraphis] refusing changed trusted config directory: $config_parent" >&2
                exit 1
            fi
            if ! chown engraphis:engraphis "$config_parent"; then
                printf '%s\n' "[engraphis] unable to own trusted config directory" >&2
                exit 1
            fi
        fi
        # Check external existing parents before creating or changing any file.
        # Parents under /data receive the volume's first-boot ownership repair.
        case "$config_parent" in
            /data|/data/*) ;;
            *)
                config_owner=$(stat -c '%u' "$config_parent" 2>/dev/null || true)
                if [ "$config_owner" != "$app_owner" ]; then
                    printf '%s\n' "[engraphis] trusted config directory must be owned by engraphis: $config_parent" >&2
                    exit 1
                fi
                ;;
        esac
        if ! reject_linked_path "$config_file"; then
            printf '%s\n' "[engraphis] refusing symlinked trusted config file: $config_file" >&2
            exit 1
        fi
        if [ ! -e "$config_file" ] && ! : > "$config_file"; then
            printf '%s\n' "[engraphis] unable to create trusted config file: $config_file" >&2
            exit 1
        fi
        if ! reject_linked_path "$config_file" || [ ! -f "$config_file" ]; then
            printf '%s\n' "[engraphis] refusing changed or non-regular trusted config file: $config_file" >&2
            exit 1
        fi
        if ! chmod 600 "$config_file"; then
            printf '%s\n' "[engraphis] unable to restrict trusted config file: $config_file" >&2
            exit 1
        fi
    fi
    # The app user owns the persistent marker after first boot. Fail closed if it has
    # replaced that trusted root-startup input with a symlink or a non-regular path:
    # chown follows symlinks by default and would otherwise let the marker redirect
    # root's ownership change to an arbitrary target on the mounted volume.
    if [ -L "$ownership_marker" ] || ! reject_linked_path "$ownership_marker"; then
        printf '%s\n' "[engraphis] refusing symlinked volume ownership marker: $ownership_marker" >&2
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
    elif [ ! -f "$ownership_marker" ]; then
        printf '%s\n' "[engraphis] refusing non-regular volume ownership marker: $ownership_marker" >&2
        exit 1
    elif ! chown engraphis:engraphis /data "$state_dir" "$ownership_marker"; then
        printf '%s\n' "[engraphis] unable to verify /data ownership" >&2
        exit 1
    fi
    if [ -n "$config_file" ]; then
        # A pre-existing config directory may be a separate root-owned mount. Do not
        # chown an arbitrary existing host path; fail closed if it is unusable instead
        # of starting a dashboard whose settings silently cannot persist.
        config_owner=$(stat -c '%u' "$config_parent" 2>/dev/null || true)
        if [ -z "$config_owner" ] || [ "$config_owner" != "$app_owner" ]; then
            printf '%s\n' "[engraphis] trusted config directory must be owned by engraphis: $config_parent" >&2
            exit 1
        fi
    fi
    if [ -n "$config_file" ]; then
        if ! reject_linked_path "$config_file" || [ ! -f "$config_file" ]; then
            printf '%s\n' "[engraphis] refusing changed trusted config file: $config_file" >&2
            exit 1
        fi
        if ! chown engraphis:engraphis "$config_file"; then
            printf '%s\n' "[engraphis] unable to own trusted config file" >&2
            exit 1
        fi
    fi
    exec gosu engraphis "$@"
fi

exec "$@"
