#!/usr/bin/env bash
# Isolated PR #147 catalog verification. Does NOT deploy or alter the live stack.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is required for isolated verification" >&2
  exit 1
fi
image="grit-wms:pr147-catalog-test"
echo "Building disposable verification image from this checked-out source..."
docker build -t "$image" "$ROOT"

# The production .dockerignore intentionally excludes tests and the app image
# runs as an unprivileged user. Mount the checkout read-only for test execution;
# -B prevents Python from trying to write __pycache__ into the image/source.
run_test() {
  docker run --rm --network none \
    --cap-drop=ALL --security-opt=no-new-privileges \
    --user 10001:10001 \
    --tmpfs /tmp:rw,nosuid,nodev,size=64m,mode=1777 \
    -v "$ROOT:/src:ro" -w /src \
    -e PYTHONPATH=/src \
    -e MOYSKLAD_TOKEN=SYNTHETIC-TOKEN \
    -e WMS_BADGE_SECRET=synthetic-badge \
    -e WMS_TERMINAL_SECRET=synthetic-terminal \
    -e SECRET_KEY=synthetic-flask \
    -e WMS_REQUIRE_POSTGRES=0 \
    --entrypoint python "$image" -B "$@"
}

echo "Checking Python syntax from the read-only checkout..."
run_test -c 'import ast,pathlib; files=("ozon_fbo_catalog_validation.py","tests/test_ozon_fbo_moysklad_diagnostics.py","tests/test_ozon_fbo_catalog_preflight.py"); [ast.parse(pathlib.Path(p).read_text(encoding="utf-8"), filename=p) for p in files]; print("Python syntax PASS")'

echo "Running mocked MoySklad pagination regression tests (network disabled)..."
run_test -m unittest discover -s /src/tests -p "test_ozon_fbo_moysklad_diagnostics.py" -v

echo "Running synthetic catalog preflight tests (network disabled)..."
run_test -m unittest discover -s /src/tests -p "test_ozon_fbo_catalog_preflight.py" -v

echo "PR147 isolated verification PASS. No application/database containers changed."
