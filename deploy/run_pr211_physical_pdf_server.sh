#!/usr/bin/env bash
# PR211 ONE synthetic PDF -> manager HTTP -> QR worker -> PG17 -> Windows session
# ONLY an ephemeral internal PostgreSQL17 and an already installed GRIT image.
# No production Compose, no WB API, no physical print on Linux.
set -Eeuo pipefail
SRC="${1:?need isolated checkout path}"
SRC="$(cd "$SRC" && pwd -P)"
case "$SRC" in /tmp/grit-pr211-physical-*) ;; *)
  echo "REFUSE_NON_ISOLATED_CHECKOUT" >&2; exit 1;; esac
test "$(git -C "$SRC" branch --show-current)" = feature/pr211-wb-real-source-preflight
test -f "$SRC/deploy/pr211_physical_pdf_session.py"
test -f "$SRC/services/printing/wb_original_print_http_synthetic.py"
test -f "$SRC/services/printing/wb_original_pg17_outbox.py"
python3 - "$SRC" <<'AST'
import ast,pathlib,sys
r=pathlib.Path(sys.argv[1])
for p in ('deploy/pr211_physical_pdf_session.py',
          'services/printing/wb_original_print_http_synthetic.py',
          'services/printing/wb_pdf_original_label.py',
          'services/printing/wb_original_pg17_outbox.py'):
    ast.parse((r/p).read_text(encoding='utf-8'),filename=p)
print('PR211 PHYSICAL PDF PYTHON SYNTAX PASS',file=sys.stderr)
AST
IMAGE=grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4
docker image inspect "$IMAGE" >/dev/null
docker image inspect postgres:17 >/dev/null
RUN="pr211physical-$(date +%s)-$$"
NET="grit-$RUN"
DB="grit-db-$RUN"
PG="grit_pr211_synthetic_$(printf '%s' "$RUN" | tr '-' '_')"
cleanup() {
  docker rm -f "$DB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT
docker network create --internal "$NET" >/dev/null
docker run -d --name "$DB" --network "$NET" --network-alias pr211-db \
  --read-only --tmpfs /tmp:rw,size=32m \
  --tmpfs /var/run/postgresql:rw,size=16m \
  --tmpfs /var/lib/postgresql/data:rw,size=256m \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB="$PG" postgres:17 >/dev/null
READY=0
for i in $(seq 1 45); do
  if docker exec "$DB" pg_isready -h 127.0.0.1 -U postgres -d "$PG" >/dev/null 2>&1; then
    READY=1; break
  fi
  sleep 1
done
if test "$READY" != 1; then
  echo "PR211 TEMPORARY PG17 NOT READY - NO PRINT" >&2
  exit 1
fi
echo "PR211 EPHEMERAL PG17 STARTED / SYNTHETIC SOURCE ONLY" >&2
# stdout is a ONE-CLAIM machine protocol to Windows. Input is ONE ACK.
# Docker never touches Windows USB. Strictly read-only source and no external
# network; tmpfs-only DB is destroyed on exit, including uncertain tickets.
docker run --rm -i --user 0:0 --network "$NET" --read-only \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m -v "$SRC:/app:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e "PR211_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr211-db:5432/$PG" \
  --entrypoint python "$IMAGE" -u /app/deploy/pr211_physical_pdf_session.py
