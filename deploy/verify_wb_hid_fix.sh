#!/usr/bin/env bash
# GRIT WB FBS Russian HID slash regression, isolated synthetic tests only.
# Never uses HOSTKEY production database, WB credentials, or external network.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
python3 - <<'PY'
import ast
from pathlib import Path
for name in ("wb_fbs_sync_v11.py", "tests/test_wb_fbs_sync_v11.py",
             "tests/test_wb_fbs_api_mock.py", "tests/test_fbs_double_product_scan.py"):
    ast.parse(Path(name).read_text(encoding="utf-8"), filename=name)
print("WB HID PYTHON SYNTAX PASS")
PY
IMAGE="grit-wb-hid-synthetic:$(git rev-parse --short HEAD)"
docker build -t "$IMAGE" .
for pattern in test_wb_fbs_sync_v11.py test_wb_fbs_api_mock.py test_fbs_double_product_scan.py; do
    echo "=== $pattern ==="
    docker run --rm --network none --read-only \
      --tmpfs /tmp:rw,nosuid,size=256m \
      -e DATABASE_URL= -e WMS_REQUIRE_POSTGRES=0 \
      -e WMS_DB_PATH=/tmp/wb-hid-synthetic.sqlite \
      -e SECRET_KEY=synthetic -e WMS_BADGE_SECRET=synthetic \
      -e WMS_TERMINAL_SECRET=synthetic \
      -e WB_API_TOKEN= -e WB_CONTENT_TOKEN= \
      -e WB_FBS_V11_ENABLED=0 \
      -e PYTHONDONTWRITEBYTECODE=1 \
      -v "$PWD/tests:/app/tests:ro" \
      --entrypoint python "$IMAGE" \
      -m unittest discover -s tests -p "$pattern" -v
done
echo "WB FBS HID SYNTHETIC UNIT PASS; production untouched."
