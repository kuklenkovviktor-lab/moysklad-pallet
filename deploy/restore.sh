#!/bin/sh
# Run inside backup container with /backups mounted, target is always NEW DB.
set -eu
[ "$#" -eq 2 ] || { echo 'Usage: restore.sh /backups/file.dump restore_check_name' >&2; exit 2; }
file=$1
target=$2
case "$file" in /backups/*.dump) ;; *) echo 'Expected /backups/*.dump' >&2; exit 2;; esac
case "$target" in restore_check_*) ;; *) echo 'Target must start with restore_check_' >&2; exit 2;; esac
case "$target" in *[!a-z0-9_]*) echo 'Unsafe database name' >&2; exit 2;; esac
pg_restore --list "$file" >/dev/null
# createdb fails if target already exists; no --clean, dropdb or overwrite.
createdb "$target"
pg_restore --exit-on-error --single-transaction --no-owner --no-acl --dbname="$target" "$file"
printf 'Restored into independent database %s; application database unchanged.\n' "$target"
