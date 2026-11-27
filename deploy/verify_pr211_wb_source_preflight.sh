#!/usr/bin/env bash
# Read-only contract + original PDF raster tests; no network / USB / live WB.
set -Eeuo pipefail
SRC="${1:?Usage: bash deploy/verify_pr211_wb_source_preflight.sh /tmp/grit-pr211-checkout}"
SRC="$(cd "$SRC" && pwd)"
for f in services/printing/wb_fbs_real_source_preflight.py \
         services/printing/wb_pdf_original_label.py \
         services/printing/wb_original_encrypted_outbox.py \
         tests/printing/test_wb_real_source_preflight.py \
         tests/printing/test_wb_pdf_original_label.py \
         tests/printing/test_wb_original_encrypted_outbox.py; do
  test -f "$SRC/$f"
done
IMAGE="grit-wms:release-1cdc36f60f549ba5a86f0ded0d728f2ae368eed4"
docker image inspect "$IMAGE" >/dev/null
for testfile in test_wb_real_source_preflight.py test_wb_pdf_original_label.py test_wb_original_encrypted_outbox.py; do
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true \
    --tmpfs /tmp:rw,size=64m -v "$SRC:/app:ro" -w /app \
    -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
    --entrypoint python "$IMAGE" \
    "/app/tests/printing/$testfile" -v
done
echo "PR211 WB PDF ORIGINAL ENCRYPTED QUEUE + BITMAP + LINE ROUTING CONTRACT PASS"
echo "SYNTHETIC ONLY; NO LIVE WB; NO PRODUCTION; NO PHYSICAL USB"
