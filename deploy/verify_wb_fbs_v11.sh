#!/usr/bin/env bash
# Offline synthetic regression: NEVER mount production data or expose WB API keys.
set -Eeuo pipefail
cd "$(dirname "$0")/.."
# Fail before building an image if the new source/tests have syntax errors.
python3 - <<'WB_FBS_AST'
import ast
from pathlib import Path
for name in (
    'app.py',
    'wms.py',
    'wb_fbs_sync_v11.py',
    'tests/test_wb_fbs_sync_v11.py',
    'tests/test_wb_v11_missing_sticker_repair.py',
):
    ast.parse(Path(name).read_text(encoding='utf-8'),filename=name)
print('WB v1.1 delayed sticker repair PYTHON SYNTAX PASS')
WB_FBS_AST
IMAGE="grit-wb-fbs-v11-synthetic:$(git rev-parse --short HEAD)"
docker build -t "$IMAGE" .
test_files=(
  test_wb_fbs_sync_v11.py
  test_wb_v11_missing_sticker_repair.py
  test_wb_fbs_api_mock.py
  test_fbs_double_product_scan.py
  test_attendance.py
  test_shift_history.py
)
for pattern in "${test_files[@]}"; do
  echo "=== RUN $pattern ==="
  docker run --rm --network none --read-only \
    --tmpfs /tmp:rw,nosuid,size=256m \
    -e DATABASE_URL= -e WMS_REQUIRE_POSTGRES=0 \
    -e WMS_DB_PATH=/tmp/wb-fbs-v11-synthetic.sqlite \
    -e SECRET_KEY=synthetic -e WMS_BADGE_SECRET=synthetic \
    -e WMS_TERMINAL_SECRET=synthetic \
    -e WB_API_TOKEN= -e WB_CONTENT_TOKEN= \
    -e WB_FBS_V11_ENABLED=0 \
    -e PYTHONDONTWRITEBYTECODE=1 \
    -v "$PWD/tests:/app/tests:ro" \
    --entrypoint python "$IMAGE" \
    -m unittest discover -s tests -p "$pattern" -v
done
echo "WB FBS v1.1 SYNTHETIC TESTS PASS. Production untouched."
