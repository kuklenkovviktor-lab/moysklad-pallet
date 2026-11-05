#!/bin/sh
set -eu
interval=${BACKUP_INTERVAL_SECONDS:-86400}
case "$interval" in ''|*[!0-9]*) echo 'Invalid backup interval' >&2; exit 1;; esac
[ "$interval" -ge 60 ] || exit 1
trap 'exit 0' TERM INT
while :; do
    if ! sh /scripts/backup-once.sh; then
        echo 'PostgreSQL backup failed; retry in 60 seconds' >&2
        sleep 60 & wait $! || true
        continue
    fi
    sleep "$interval" & wait $! || true
done
