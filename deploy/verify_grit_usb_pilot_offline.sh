#!/usr/bin/env bash
# Offline Python tests. FAKE Windows spooler only; never USB, WB or production.
set -Eeuo pipefail
SRC="$1"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/services/printing/grit_usb_print_pilot.py"
test -f "$SRC/tests/printing/test_grit_usb_print_pilot.py"
python3 -m py_compile "$SRC/services/printing/grit_usb_print_pilot.py" "$SRC/tests/printing/test_grit_usb_print_pilot.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC" python3 \
  "$SRC/tests/printing/test_grit_usb_print_pilot.py" -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$SRC" python3 \
  "$SRC/services/printing/grit_usb_print_pilot.py" --preview \
  | grep -q "SIZE 58 mm,40 mm"
echo "GRIT USB 58X40 FAKE SPOOLER / NO HARDWARE PASS"
