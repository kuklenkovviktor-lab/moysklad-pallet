#!/usr/bin/env bash
# HOSTKEY standalone synthetic tests. No production database or live containers.
set -Eeuo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"
expected="feature/map-label-reference-v2"
[[ "$(git rev-parse --abbrev-ref HEAD)" == "$expected" ]] || { echo "Wrong branch" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Dirty source checkout" >&2; exit 1; }
sha="$(git rev-parse HEAD)"
image="grit-wms:map-label-v2-test-$sha"
echo "Building isolated test image at $sha"
docker build -t "$image" "$root"
for file in test_warehouse_map_labels_75x120.py test_warehouse_map.py test_qr_cell_58x40.py test_cell_audit.py
do
  echo "RUN $file (synthetic, no network)"
  docker run --rm --network none --read-only \
    --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:rw,noexec,nosuid,size=192m \
    -e WMS_REQUIRE_POSTGRES=0 -e WMS_BADGE_SECRET=synthetic-badge \
    -e WMS_TERMINAL_SECRET=synthetic-terminal \
    -e SECRET_KEY=synthetic-session -e MOYSKLAD_TOKEN=synthetic \
    -v "$root/tests:/app/tests:ro" \
    --entrypoint python "$image" -m unittest discover -s tests -p "$file" -v
done
echo "MAP LABEL V2 ISOLATED TEST PASS: $sha"
