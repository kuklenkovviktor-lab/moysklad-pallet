#!/usr/bin/env bash
# PR211: read-only local fixture tests. No database/production/network/device.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_pr211_wb_source_preflight.sh /checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/services/printing/wb_fbs_real_source_preflight.py"
test -f "$SRC/tests/printing/test_wb_real_source_preflight.py"
PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 python3   "$SRC/tests/printing/test_wb_real_source_preflight.py" -v
echo "PR211 REAL WB TABLE CONTRACT SYNTHETIC FIXTURES PASS"
echo "READ-ONLY; NO PRINT; NO REAL ORDERS; NO DATABASE CONNECTION"
