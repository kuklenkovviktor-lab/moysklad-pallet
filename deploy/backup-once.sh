#!/bin/sh
set -eu
umask 077
: "${PGHOST:?}" "${PGUSER:?}" "${PGDATABASE:?}" "${PGPASSWORD:?}"
retention=${BACKUP_RETENTION_DAYS:-14}
case "$retention" in ''|*[!0-9]*) echo 'Invalid retention' >&2; exit 1;; esac
[ "$retention" -ge 1 ] || exit 1
# Unique temp name; never expose a partial dump as a completed backup.
tmp=$(mktemp /backups/.grit-XXXXXX.partial)
trap 'rm -f "$tmp"' EXIT HUP INT TERM
pg_dump --format=custom --no-owner --no-acl --file="$tmp"
pg_restore --list "$tmp" >/dev/null
name="/backups/grit-$(date -u +%Y%m%dT%H%M%SZ)-$(basename "$tmp" .partial).dump"
mv "$tmp" "$name"
date +%s > /backups/.last-success.tmp
mv /backups/.last-success.tmp /backups/.last-success
# Retention runs only after a successful, readable dump.
find /backups -maxdepth 1 -type f -name 'grit-*.dump' -mtime +"$retention" -delete
printf 'PostgreSQL backup completed: %s\n' "$(basename "$name")"
