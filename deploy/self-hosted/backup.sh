#!/bin/sh
# Server-side PostgreSQL 16 backup. MUST ALSO copy offsite and test restore.
set -eu
umask 077
here="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
destination="$here/backups"
mkdir -p "$destination"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
temp="$(mktemp "$destination/.onix-$timestamp.XXXXXX")"
trap 'rm -f "$temp"' EXIT HUP INT TERM
docker compose --project-directory "$here" -f "$here/compose.yml" exec -T db sh -c 'PGPASSWORD="$(cat /run/secrets/postgres_password)" pg_dump -h 127.0.0.1 -U onix -d onix -Fc --no-owner --no-acl' > "$temp"
# Validate archive's table of contents before making it a retained backup.
docker compose --project-directory "$here" -f "$here/compose.yml" exec -T db pg_restore --list < "$temp" >/dev/null
mv -- "$temp" "$destination/onix-$timestamp.dump"
trap - EXIT HUP INT TERM
printf 'Backup verified locally: %s\n' "$destination/onix-$timestamp.dump"
printf 'IMPORTANT: copy encrypted offsite; a single VPS disk is not a backup strategy.\n'
