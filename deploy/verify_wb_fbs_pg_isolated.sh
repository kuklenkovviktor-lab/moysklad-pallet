#!/usr/bin/env bash
# Independent disposable PostgreSQL test. Does not join GRIT production networks.
set -Eeuo pipefail
cd "$(dirname "$0")/.."

IMAGE="grit-wb-fbs-v11-synthetic:$(git rev-parse --short HEAD)"
NET="grit-wb-pg-only-$$"
PG="grit-wb-pg-test-$$"

cleanup() {
  rc=$?
  trap - EXIT
  docker rm -f "$PG" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  exit "$rc"
}
trap cleanup EXIT

if [[ -n "$DATABASE_URL" || -n "$WB_API_TOKEN" || -n "$WB_CONTENT_TOKEN" ]]; then
  echo "STOP: unset DATABASE_URL, WB_API_TOKEN and WB_CONTENT_TOKEN in your shell first." >&2
  exit 2
fi

echo "Creating private internal Docker network, with NO production mounts."
docker network create --internal "$NET" >/dev/null
docker run -d --rm \
  --name "$PG" --network "$NET" --network-alias pg \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=synthetic-only \
  -e POSTGRES_DB=synthetic -e PGDATA=/var/lib/postgresql/data/pgdata \
  postgres:17-alpine >/dev/null

ready=0
for _ in $(seq 1 45); do
  if docker exec "$PG" pg_isready -U postgres -d synthetic >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" != 1 ]]; then
  echo "FAIL: synthetic PostgreSQL did not start" >&2
  docker logs "$PG" 2>&1 | tail -n 35 >&2
  exit 1
fi

docker build -t "$IMAGE" .

echo "=== POSTGRES ISOLATED WB FBS STRESS TEST ==="
docker run --rm --network "$NET" --read-only \
  --tmpfs /tmp:rw,nosuid,size=256m \
  -e GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED \
  -e DATABASE_URL=postgresql://postgres:synthetic-only@pg:5432/synthetic \
  -e WMS_REQUIRE_POSTGRES=1 \
  -e WMS_BADGE_SECRET=stress-synthetic-badge \
  -e WMS_TERMINAL_SECRET=stress-synthetic-terminal \
  -e WB_FBS_V11_ENABLED=1 \
  -e WB_API_TOKEN= -e WB_CONTENT_TOKEN= \
  -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python "$IMAGE" \
  -m unittest discover -s tests -p test_wb_fbs_pg_stress.py -v

echo "WB FBS v1.1 POSTGRES STRESS PASS. Disposable test DB/network destroyed on exit."
