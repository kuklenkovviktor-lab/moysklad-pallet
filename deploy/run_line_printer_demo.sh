#!/usr/bin/env bash
# Disposable GRIT PR192 demo; no access to production containers/DB.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/run_line_printer_demo.sh /source/checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/line_printer_demo_app.py"
STATE="${GRIT_PR192_DEMO_STATE:-/tmp/grit-pr192-demo.state}"
# Deliberately support only two known disposable demo state files.
# Never follow symlinks, and never infer container names from production state.
case "$STATE" in
  /tmp/grit-pr192-demo.state|/tmp/grit-pr204-demo.state|/tmp/grit-pr205-demo.state|/tmp/grit-pr206-demo.state|/tmp/grit-pr208-demo.state) ;;
  *) echo "Refusing unexpected synthetic demo state path" >&2; exit 2;;
esac
if test -L "$STATE"; then
  echo "Refusing symlink demo state path" >&2
  exit 2
fi
if test -e "$STATE"; then
  echo "Demo state file already exists at $STATE; refusing to replace it."
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
python3 - "$SRC" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
for path in ("line_printer_demo_app.py", "line_printer_http_synthetic.py",
             "line_printer_pg_integrated_trial.py", "line_printer_pg_lifecycle.py",
             "line_printer_wms_auth_adapter.py"):
    compile((root/path).read_text(encoding="utf-8"), str(root/path), "exec")
print("PR192 PYTHON SYNTAX PASS")
PY
docker image inspect "$IMAGE" >/dev/null
PASS="$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')"
SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
# One-time device credential scoped to this disposable synthetic demo only.
USB_AGENT_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(40))')"
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
# A Docker --internal bridge can make host-published loopback ports unreachable.
# This is a dedicated bridge, attached ONLY to synthetic demo containers.
# No production containers, credentials, or WB tokens are present.
docker network create --driver bridge "$NET" >/dev/null
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
  --no-healthcheck \
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
  -e "GRIT_PR192_USB_AGENT_TOKEN=$USB_AGENT_TOKEN" \
  --entrypoint python "$IMAGE" \
  -m gunicorn --bind 0.0.0.0:8000 --workers 1 --timeout 60 \
  --access-logfile - --error-logfile - line_printer_demo_app:app >/dev/null
READY=0
for i in $(seq 1 60); do
  if curl -fs --max-time 3 "http://127.0.0.1:${PORT}/health" \
      | grep -q '"ok":true'; then READY=1; break; fi
  if [ "$(docker inspect --format '{{.State.Running}}' "$APP" 2>/dev/null || true)" != "true" ]; then
    echo "Demo application container exited before readiness."
    docker logs "$APP" --tail 100 2>&1 || true
    exit 1
  fi
  sleep 1
done
if [ "$READY" != 1 ]; then
  echo "Demo HTTP health probe FAILED on loopback port $PORT."
  docker inspect "$APP" --format 'APP={{.State.Status}} EXIT={{.State.ExitCode}} NETWORK={{.HostConfig.NetworkMode}} PORTS={{json .NetworkSettings.Ports}}' || true
  docker logs "$APP" --tail 100 2>&1 || true
  exit 1
fi
GRIT_PR192_DEMO_PORT="$PORT" GRIT_PR192_DEMO_PASSWORD="$PASS" \
  python3 "$SRC/deploy/smoke_line_printer_demo.py"
(umask 077; printf 'NET=%q\nDB=%q\nAPP=%q\nPORT=%q\n' \
  "$NET" "$DB" "$APP" "$PORT" > "$STATE")
CREATED=1
echo "DEMO START PASS: isolated PostgreSQL 17 + WMS auth + browser UI"
echo "HOSTKEY: http://127.0.0.1:${PORT}/demo"
echo "Manager demo password: $PASS"
echo "Worker synthetic badge: DEMO-BADGE-1"
echo "Disposable USB agent token (enter on permitted test PC only): $USB_AGENT_TOKEN"
echo "PC tunnel: ssh -L ${PORT}:127.0.0.1:${PORT} root@141.105.66.249"
echo "PC browser: http://127.0.0.1:${PORT}/demo"
echo "STOP: GRIT_PR192_DEMO_STATE=$STATE bash $SRC/deploy/stop_line_printer_demo.sh"
echo "Only synthetic manager-authorized USB demo jobs; NO PRODUCTION DB/WB API."
