#!/bin/sh
# Starts as root only to fix folder owners, then runs the app as user "app".
set -e

if [ "$(id -u)" = "0" ]; then
    # Optional: use your own user / group id for the files (PUID / PGID).
    [ -n "$PGID" ] && groupmod -o -g "$PGID" app
    [ -n "$PUID" ] && usermod -o -u "$PUID" app

    # Docker creates missing bind-mount folders as root. Give them to "app".
    for d in "$RECORDINGS_DIR" "$DATA_DIR"; do
        mkdir -p "$d"
        chown "$(id -u app):$(id -g app)" "$d"
    done

    # Keep the extra groups from docker-compose "group_add" (e.g. the gid of
    # /dev/snd), but not the root group.
    groups=$(id -G | tr ' ' '\n' | grep -vx 0 | paste -sd, -)
    audio_gid=$(getent group audio | cut -d: -f3)
    exec setpriv --reuid="$(id -u app)" --regid="$(id -g app)" \
        --groups="${groups:+$groups,}$audio_gid" env HOME=/home/app "$@"
fi

exec "$@"
