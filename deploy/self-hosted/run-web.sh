#!/bin/sh
# Self-hosted ONIX: mandatory secrets and PostgreSQL only.
set -eu
umask 077

read_required() {
  if [ ! -r "$1" ]; then
    echo "Missing required secret: $1" >&2
    exit 1
  fi
  value="$(cat "$1")"
  if [ -z "$value" ]; then
    echo "Empty required secret: $1" >&2
    exit 1
  fi
  printf %s "$value"
}

export SECRET_KEY="$(read_required /run/secrets/flask_secret)"
export WMS_BADGE_SECRET="$(read_required /run/secrets/badge_secret)"
export WMS_TERMINAL_SECRET="$(read_required /run/secrets/terminal_secret)"
export MANAGER_WEB_PASSWORD="$(read_required /run/secrets/manager_password)"
export MOYSKLAD_TOKEN="$(read_required /run/secrets/moysklad_token)"
export WB_API_TOKEN="$(cat /run/secrets/wb_api_token)"
pw="$(read_required /run/secrets/postgres_password)"
export DATABASE_URL="$(POSTGRES_PASSWORD="$pw" python -c 'import os; from urllib.parse import quote; print("postgresql://onix:"+quote(os.environ["POSTGRES_PASSWORD"],safe="")+"@db:5432/onix")')"
unset pw
export WMS_REQUIRE_POSTGRES=1
export WMS_MIGRATE_SQLITE_FROM_S3=0

exec gunicorn --bind 0.0.0.0:8000 --worker-class gthread --workers 1 --threads 8 --timeout 120 app:app
