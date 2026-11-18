#!/usr/bin/env bash
# Run isolated Ozon FBO tests on HOSTKEY without using GitHub Actions.
# Usage: bash deploy/hostkey_catalog_ui_test.sh
set -Eeuo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
expected_branch="fix/ozon-fbo-catalog-search-button"

[[ "$(git rev-parse --abbrev-ref HEAD)" == "$expected_branch" ]] || {
  echo "BLOCKED: checkout $expected_branch before testing" >&2
  exit 1
}
[[ -z "$(git status --porcelain)" ]] || {
  echo "BLOCKED: test checkout contains uncommitted changes" >&2
  exit 1
}
sha="$(git rev-parse HEAD)"
image="${GRIT_TEST_IMAGE:-grit-wms:release-4e07408eaa59768de7e66ab00ea5f06e2bf6bb71}"
docker image inspect "$image" >/dev/null || {
  echo "BLOCKED: installed test image not found: $image" >&2
  exit 1
}
echo "TEST SOURCE: $sha"
echo "TEST IMAGE: $image"
echo "ISOLATION: --network none; no host DB socket; no secrets or production env mounts"
for test_file in \
  test_fbs_persistent_catalog.py \
  test_ozon_fbo_catalog_preflight.py \
  test_ozon_fbo_shortages.py \
  test_ozon_fbo_picking.py \
  test_ozon_fbo_parser.py \
  test_ozon_fbo_moysklad_diagnostics.py
do
  echo "RUN: $test_file"
  docker run --rm \
    --network none \
    --read-only \
    --cap-drop ALL \
    --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=192m \
    -e WMS_REQUIRE_POSTGRES=0 \
    -e WMS_BADGE_SECRET=synthetic-badge \
    -e WMS_TERMINAL_SECRET=synthetic-terminal \
    -e SECRET_KEY=synthetic-session \
    -e MOYSKLAD_TOKEN=synthetic \
    -v "$root:/app:ro" \
    -w /app \
    --entrypoint python \
    "$image" -m unittest discover -s tests -p "$test_file" -v
done
echo "CATALOG UI ISOLATED TEST PASS: $sha"
