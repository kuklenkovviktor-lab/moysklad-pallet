#!/usr/bin/env bash
# Completely offline / no hardware / fake Windows print spooler only.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_grit_usb_bridge_offline.sh /checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/services/printing/grit_windows_usb_agent.py"
test -f "$SRC/services/printing/grit_usb_print_pilot.py"
test -f "$SRC/tests/printing/test_grit_windows_usb_agent.py"
python3 -B - "$SRC" <<'PY'
from pathlib import Path
import sys
root=Path(sys.argv[1])
for relative in (
    "services/printing/grit_windows_usb_agent.py",
    "services/printing/usb_agent_config.py",
    "services/printing/usb_test_preparation.py",
    "deploy/prepare_usb_physical_test.py",
    "services/printing/usb_bridge_synthetic.py",
    "tests/printing/test_grit_windows_usb_agent.py",
    "tests/printing/test_usb_bridge_synthetic.py",
    "line_printer_demo_app.py",
):
    compile((root/relative).read_text(encoding="utf-8"),relative,"exec")
print("GRIT BRIDGE PYTHON SYNTAX PASS")
PY
PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B "$SRC/tests/printing/test_grit_windows_usb_agent.py" -v
PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B "$SRC/tests/printing/test_usb_agent_config.py" -v
echo "GRIT WINDOWS BRIDGE FAKE SPOOLER PASS; NO USB, NO NETWORK"
