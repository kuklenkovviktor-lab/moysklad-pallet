#!/usr/bin/env bash
# Isolated PR #147 catalog verification. Does NOT deploy or alter the live stack.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for isolated verification" >&2
  exit 1
fi
image="grit-wms:pr147-catalog-test"
echo "Building disposable verification image from this checked-out source..."
docker build -t "$image" .
echo "Checking Python compilation without access to the production stack..."
docker run --rm --network none --entrypoint python "$image" \
  -m compileall -q ozon_fbo_catalog_validation.py tests/test_ozon_fbo_moysklad_diagnostics.py
echo "Running mocked MoySklad regression tests (network disabled)..."
docker run --rm --network none \
  -e MOYSKLAD_TOKEN=SYNTHETIC-TOKEN \
  -e WMS_BADGE_SECRET=synthetic-badge \
  -e WMS_TERMINAL_SECRET=synthetic-terminal \
  -e SECRET_KEY=synthetic-flask \
  -e WMS_REQUIRE_POSTGRES=0 \
  --entrypoint python "$image" \
  -m unittest -v tests.test_ozon_fbo_moysklad_diagnostics tests.test_ozon_fbo_catalog_preflight
echo "PR147 isolated verification PASS. No application/database containers changed."
