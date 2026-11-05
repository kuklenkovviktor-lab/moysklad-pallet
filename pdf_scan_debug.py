"""Temporary, read-only GRIT PDF scan diagnostic for an observed production bug.

Reads only existing rejected order sticker events from the current database.
Does not scan, mutate data, make external calls, or inspect employee badges.
Remove after fixing the scanner mapping.
"""
import json

def summarize_rejected_scans():
    from wms import query_all, query_one
    from wb_qr_codec import canonical_wb_sticker
    count=query_one("SELECT COUNT(*) FROM pdf_fbs_orders")
    print("PDF SCAN DIAGNOSTIC registry_count=" + str(count[0] if count else "unknown"), flush=True)
    rows=query_all(
        "SELECT object_code, article, created_at FROM operation_events "
        "WHERE event_type='pdf_order_missing' "
        "ORDER BY created_at DESC LIMIT 16"
    )
    for raw, article, created_at in rows:
        s=str(raw or "")
        if s.startswith(("WMS-BADGE", "цьы-ифвпу")) or len(s)>80:
            s="<redacted>"
        canonical=canonical_wb_sticker(s)
        found=False
        if canonical and canonical!="<redacted>":
            found=bool(query_one("SELECT 1 FROM pdf_fbs_orders WHERE wb_barcode=?",(canonical,)))
        print("PDF SCAN DIAGNOSTIC "+json.dumps({
            "at":str(created_at),"source":s,
            "length":len(s),"decoded":canonical,
            "recognized":found,"article":str(article or "")[:40]
        },ensure_ascii=False),flush=True)
