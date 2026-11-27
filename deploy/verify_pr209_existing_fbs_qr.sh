#!/usr/bin/env bash
# PR209: test existing /fbs QR scanner + synthetic routing only.
# Runs no production deploy, migrations, WB API, real printer, or real FBS orders.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_pr209_existing_fbs_qr.sh /checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/wms.py"
test -f "$SRC/tests/printing/test_existing_fbs_printer_qr.cjs"
test -f "$SRC/tests/printing/test_existing_fbs_real_wms_pg_isolated.py"
test -f "$SRC/deploy/verify_line_printer_print_queue_pg_isolated.sh"
# Use a disposable Node container if Node is not installed on HOSTKEY.
# Keep the repository readonly; no volumes except checkout, no network for JS.
if command -v node >/dev/null 2>&1; then
  js_test() { node "$SRC/tests/printing/test_existing_fbs_printer_qr.cjs"; }
else
  command -v docker >/dev/null 2>&1 || {
    echo "BLOCKED: neither Node.js nor Docker available; no deploy performed." >&2
    exit 2
  }
  js_test() {
    docker run --rm --network none --read-only --cap-drop ALL \
      --security-opt no-new-privileges:true \
      --tmpfs /tmp:rw,size=16m \
      --user 65534:65534 \
      -v "$SRC:/app:ro" -w /app \
      node:22-alpine node tests/printing/test_existing_fbs_printer_qr.cjs
  }
fi
python3 - "$SRC" <<'PY'
import ast
from pathlib import Path
import sys
root=Path(sys.argv[1])
for name in ("wms.py", "line_printer_demo_app.py",
             "tests/printing/test_existing_fbs_real_wms_pg_isolated.py",
             "deploy/smoke_line_printer_demo.py"):
    ast.parse((root/name).read_text(encoding="utf-8"),filename=name)
print("PR209 PYTHON SYNTAX PASS")
PY
js_test
echo "PR209 FBS EXISTING UI JS PASS"
bash "$SRC/deploy/verify_line_printer_print_queue_pg_isolated.sh" "$SRC"
echo "PR209 EXISTING FBS QR + ISOLATED PG17 REGRESSIONS PASS"
echo "SYNTHETIC ONLY; PRODUCTION AND PHYSICAL USB UNCHANGED"
