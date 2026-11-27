#!/usr/bin/env bash
# PR210: isolated-only full regression, never installs packages/deploys.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_pr210_auto_usb_synthetic.sh /tmp/grit-pr210-checkout}"
SRC="$(cd "$SRC" && pwd)"
test -f "$SRC/deploy/arm_pr210_auto_demo.py"
test -f "$SRC/tests/printing/test_pr210_full_virtual_fbs_to_windows.py"
python3 - "$SRC" <<'PY'
import ast
from pathlib import Path
import sys
root=Path(sys.argv[1])
for file in ("services/printing/usb_bridge_synthetic.py",
             "services/printing/qr_http_synthetic.py",
             "services/printing/grit_windows_usb_agent.py",
             "deploy/arm_pr210_auto_demo.py",
             "tests/printing/test_usb_bridge_synthetic.py",
             "tests/printing/test_grit_windows_usb_agent.py",
             "tests/printing/test_pr210_full_virtual_fbs_to_windows.py"):
    ast.parse((root/file).read_text(encoding="utf-8"),filename=file)
print("PR210 PYTHON SYNTAX PASS")
PY
bash "$SRC/deploy/verify_pr209_existing_fbs_qr.sh" "$SRC"
IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"
docker image inspect "$IMAGE" >/dev/null
docker run --rm --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,size=64m -v "$SRC:/app:ro" -w /app \
  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
  --entrypoint python "$IMAGE" \
  /app/tests/printing/test_grit_windows_usb_agent.py -v
echo "PR210 AUTO USB SERVER + WINDOWS FAKE-SPOOLER ISOLATED PASS"
echo "PR210 VIRTUAL SCANNER-TO-SPOOL COMPLETE; NO MANAGER UI OR PHYSICAL SCANNER"
echo "SYNTHETIC ONLY; NO DEPLOY; NO USB; NO LIVE WB"
