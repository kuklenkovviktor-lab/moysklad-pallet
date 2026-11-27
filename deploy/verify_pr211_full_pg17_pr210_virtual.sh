#!/usr/bin/env bash
# PR211: single synthetic-only runner, disposable PG17 + original-PDF +
# actual PR210 one-write RAW routine with FAKE Windows printer.
# No production Compose, persistent volumes, USB, WB API or network outside
# an internal disposable Docker network.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_pr211_full_pg17_pr210_virtual.sh /tmp/pr211-checkout}"
SRC="$(cd "$SRC" && pwd -P)"
for path in \
    services/printing/wb_original_pg17_outbox.py \
    services/printing/wb_pdf_original_label.py \
    services/printing/grit_usb_print_pilot.py \
    tests/printing/test_wb_original_pg17_pr210_end_to_end.py; do
    test -f "$SRC/$path"
done
IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"
docker image inspect "$IMAGE" >/dev/null
docker image inspect postgres:17 >/dev/null
RUN="pr211-$(date +%s)-$$"
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
        READY=1
        break
    fi
    sleep 1
done
if test "$READY" != 1; then
    echo "DISPOSABLE POSTGRESQL17 NOT READY — NO TEST RUN" >&2
    exit 1
fi
echo "=== PR211 ORIGINAL WB SOURCE + ENCRYPTED SQLITE CONTRACT / OFFLINE ==="
bash "$SRC/deploy/verify_pr211_wb_source_preflight.sh" "$SRC"
echo "=== PR211 PDF -> EXACT WB ORDER + LINE -> PG17 -> PR210 FAKE RAW USB ==="
docker run --rm --user 0:0 --network "$NET" --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,size=128m -v "$SRC:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
    -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
    -e "PR211_SYNTHETIC_DATABASE_URL=postgresql://postgres:synthetic-only@pr211-db:5432/$PG" \
    --entrypoint python "$IMAGE" \
    /app/tests/printing/test_wb_original_pg17_pr210_end_to_end.py -v
echo "PR211 PG17 ORIGINAL PDF TO PR210 WINDOWS RAW FAKE-SPOOLER PASS"
echo "SYNTHETIC ONLY / NO USB / NO WB API / NO PRODUCTION DEPLOY"
