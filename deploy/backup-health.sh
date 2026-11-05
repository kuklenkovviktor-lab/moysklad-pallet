#!/bin/sh
set -eu
last=$(cat /backups/.last-success)
interval=${BACKUP_INTERVAL_SECONDS:-86400}
now=$(date +%s)
[ "$last" -le "$now" ]
[ "$((now-last))" -le "$((interval+300))" ]
