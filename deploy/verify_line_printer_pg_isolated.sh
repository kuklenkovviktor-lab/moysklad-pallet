#!/usr/bin/env bash
# Destructive ONLY to disposable containers/network created by this script.
# Never accepts production DB names, volumes or external connection strings.
set -Eeuo pipefail

SRC="${1:?Usage: bash deploy/verify_line_printer_pg_isolated.sh /path/to/checked-out-branch}"
test -f "$SRC/tests/test_line_printer_pg_trial.py"
test -f "$SRC/line_printer_pg_trial.py"

RUN="pr192-$(date +%s)-$$"
RUN="${RUN//[^a-zA-Z0-9-]/}"
NETWORK="grit-${RUN}"
DB_CONTAINER="grit-pg-${RUN}"
DB_NAME="grit_pr192_synthetic_${RUN//-/_}"
APP_IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"

cleanup() {
  docker rm -f "$DB_CONTAINER" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "=== ISOLATED PG17 NETWORK ==="
docker network create --internal "$NETWORK" >/dev/null

echo "=== EPHEMERAL POSTGRESQL 17 ==="
docker run -d --name "$DB_CONTAINER" \
  --network "$NETWORK" \
  --network-alias pr192-db \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB="$DB_NAME" \
  --tmpfs /var/lib/postgresql/data:rw,size=256m \
  postgres:17 >/dev/null

READY=0
for i in $(seq 1 45); do
  if docker exec "$DB_CONTAINER" pg_isready -U postgres -d "$DB_NAME" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
test "$READY" = 1

echo "=== SYNTHETIC PG17 ROUTING TESTS ==="
docker run --rm \
  --network "$NETWORK" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m \
  -v "$SRC:/app:ro" \
  -w /app \
  -e PYTHONPATH=/app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR192_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr192-db:5432/$DB_NAME" \
  --entrypoint python \
  "$APP_IMAGE" \
  /app/tests/test_line_printer_pg_trial.py -v

echo "=== SYNTHETIC PG17 INTEGRATION TESTS ==="
docker run --rm \
  --network "$NETWORK" \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m \
  -v "$SRC:/app:ro" \
  -w /app \
  -e PYTHONPATH=/app \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR192_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr192-db:5432/$DB_NAME" \
  --entrypoint python \
  "$APP_IMAGE" \
  /app/tests/test_line_printer_pg_integrated_trial.py -v

echo "PR192 PG17 ISOLATED TEST PASS"
