#!/usr/bin/env bash
# Disposable PostgreSQL17 experiment. No prod Compose, volumes or external DB.
set -Eeuo pipefail
SRC="$1"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/tests/printing/test_pg_queue.py"
test -f "$SRC/services/printing/pg_queue.py"
RUN="pr192queue-$(date +%s)-$$"
NET="grit-$RUN"
DB="grit-db-$RUN"
PG_NAME="grit_pr192_synthetic_$(printf '%s' "$RUN" | tr '-' '_')"
IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"

cleanup() {
  docker rm -f "$DB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker image inspect "$IMAGE" >/dev/null
docker network create --internal "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias pr192-db \
  --read-only --tmpfs /tmp:rw,size=32m \
  --tmpfs /var/run/postgresql:rw,size=16m \
  --tmpfs /var/lib/postgresql/data:rw,size=256m \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB="$PG_NAME" postgres:17 >/dev/null
READY=0
for i in $(seq 1 45); do
  if docker exec "$DB" pg_isready -h 127.0.0.1 -U postgres -d "$PG_NAME" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if test "$READY" != 1; then
  echo "Isolated PG17 not ready" >&2
  exit 1
fi
docker run --rm --network "$NET" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m -v "$SRC:/app:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR192_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr192-db:5432/$PG_NAME" \
  --entrypoint python "$IMAGE" \
  /app/tests/printing/test_pg_queue.py -v
echo "=== SYNTHETIC QR -> AUTO-QUEUE HTTP + POSTGRESQL 17 ==="
docker run --rm --network "$NET" --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m -v "$SRC:/app:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR192_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr192-db:5432/$PG_NAME" \
  --entrypoint python "$IMAGE" \
  /app/tests/printing/test_qr_autoqueue.py -v
echo "PR192 SYNTHETIC QR AUTOQUEUE PG17 PASS"
echo "PR192 PRINT QUEUE PG17 ISOLATED PASS"
