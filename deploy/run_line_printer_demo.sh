#!/usr/bin/env bash
# Disposable GRIT PR192 demo; no access to production containers/DB.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/run_line_printer_demo.sh /source/checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/line_printer_demo_app.py"
STATE="/tmp/grit-pr192-demo.state"
if test -e "$STATE"; then
  echo "Demo already exists. Stop with bash $SRC/deploy/stop_line_printer_demo.sh"
  exit 2
fi
RUN="pr192demo-$(date +%s)-$$"
NET="grit-$RUN"
DB="grit-db-$RUN"
APP="grit-ui-$RUN"
PG_NAME="grit_pr192_synthetic_${RUN//-/_}"
IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"
PORT="${GRIT_PR192_DEMO_PORT:-18765}"
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] || exit 2
docker image inspect "$IMAGE" >/dev/null
PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
CREATED=0
cleanup_failed() {
  if [ "$CREATED" != 1 ]; then
    docker logs "$APP" --tail 60 2>/dev/null || true
    docker rm -f "$APP" "$DB" >/dev/null 2>&1 || true
    docker network rm "$NET" >/dev/null 2>&1 || true
    rm -f "$STATE"
  fi
}
trap cleanup_failed EXIT
docker network create --internal "$NET" >/dev/null
docker run -d --name "$DB" \
  --network "$NET" --network-alias pr192-db \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB="$PG_NAME" \
  --tmpfs /var/lib/postgresql/data:rw,size=256m \
  postgres:17 >/dev/null
READY=0
for i in $(seq 1 60); do
  if docker exec "$DB" pg_isready -h 127.0.0.1 -U postgres \
      -d "$PG_NAME" >/dev/null 2>&1; then READY=1; break; fi
  sleep 1
done
test "$READY" = 1
docker run -d --name "$APP" \
  --network "$NET" \
  --read-only --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=128m \
  -p "127.0.0.1:${PORT}:8000" \
  -v "$SRC:/app:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR192_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr192-db:5432/$PG_NAME" \
  -e WMS_BADGE_SECRET=synthetic-pr192-badge \
  -e WMS_TERMINAL_SECRET=synthetic-pr192-terminal \
  -e WMS_DB_PATH=/tmp/grit-pr192-demo.sqlite \
  -e WMS_REQUIRE_POSTGRES=0 -e DATABASE_URL= \
  -e "GRIT_PR192_DEMO_PASSWORD=$PASS" \
  -e "GRIT_PR192_DEMO_SECRET=$SECRET" \
  --entrypoint python "$IMAGE" \
  -m gunicorn --bind 0.0.0.0:8000 --workers 1 --timeout 60 \
  --access-logfile - --error-logfile - line_printer_demo_app:app >/dev/null
READY=0
for i in $(seq 1 60); do
  if curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/health" \
      | grep -q '"ok":true'; then READY=1; break; fi
  sleep 1
done
test "$READY" = 1
(umask 077; printf 'NET=%q\nDB=%q\nAPP=%q\nPORT=%q\n' \
  "$NET" "$DB" "$APP" "$PORT" > "$STATE")
CREATED=1
echo "DEMO START PASS: isolated PostgreSQL 17 + WMS auth + browser UI"
echo "HOSTKEY: http://127.0.0.1:${PORT}/demo"
echo "Manager demo password: $PASS"
echo "Worker synthetic badge: DEMO-BADGE-1"
echo "PC tunnel: ssh -L ${PORT}:127.0.0.1:${PORT} root@141.105.66.249"
echo "PC browser: http://127.0.0.1:${PORT}/demo"
echo "STOP: bash $SRC/deploy/stop_line_printer_demo.sh"
echo "No production DB, WB API or real printer jobs."
