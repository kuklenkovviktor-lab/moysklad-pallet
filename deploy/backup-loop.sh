#!/bin/sh
set -eu
exec python3 /scripts/backup_worker.py loop
