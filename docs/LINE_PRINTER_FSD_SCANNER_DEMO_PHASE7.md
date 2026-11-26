# GRIT WMS — FSD scanner demo (synthetic-only)

2026-11-26. Related issue #192, parent PR #203.

## Modules

- frontend/features/printer_scan/scan.js: QR-prefix recognizer, session-bound CSRF, QR-only HTTP POST, HID Enter handling and bounded input buffer. No order selection.
- frontend/pages/printer_scan_demo/index.html: minimal dark scanning page, explicitly labeled SYNTHETIC. Not the live /fbs page.
- services/printing/worker_ui_synthetic.py: guarded Flask blueprint, GET /demo/worker-qr and GET JS asset only for verified employee in active shift.
- services/printing/qr_http_synthetic.py: GET /synthetic/pr192/print-queue/session issues CSRF only to session-verified worker.
- line_printer_demo_app.py: demo-only wiring and invented WB sticker fixture. app.py and wms.py unchanged.
- tests/printing/test_worker_scanner_demo.py: 13 new real-WMS-auth + PG17 HTTP tests.
- deploy/smoke_line_printer_demo.py: standalone localhost HTTP smoke extended with QR autoqueue and idempotent rescan.

## Manual demo (only after isolated verification)

1. Start isolated standalone demo via deploy/run_line_printer_demo.sh (binds loopback on HOSTKEY; never production) if no other demo occupies the state file.
2. In /demo manager mode, create printer for А,Б lines and copy only the INVENTED test QR string.
3. Log in with synthetic DEMO-BADGE-1 worker badge, open /demo/worker-qr.
4. Enter or scan the invented QR + Enter. Server finds local synthetic eligible orders and enqueues them. Repeated QR does not duplicate.
5. Screen displays enqueue counters, NOT physical printing. No real WB orders or Xprinter.

## Regression and remaining work

Previous PR #203 passed 92 unique synthetic checks. The new 13 tests and updated standalone demo smoke are NOT RUN at PR creation. Re-run deploy/verify_line_printer_print_queue_pg_isolated.sh on disposable HOSTKEY PostgreSQL17, expecting PR192 FSD WORKER SCANNER DEMO PG17 PASS only after actual success.

This PR does NOT change live /fbs or install scanner hook in wms.py. Final integration into existing handleScan(code) must happen AFTER durable FBS-outbox replay, and must reject printer QR while pendingProduct exists, preserving the pending WB scan. Ordinary product/WB/skip scans must remain unchanged.

No real physical USB printing, no real WB sticker or external API, no production schema or routes, no automatic merge or deployment.
## Parallel demo on an occupied HOSTKEY

If existing /tmp/grit-pr192-demo.state (port 18765) exists, leave it untouched. PR204 launch supports a separate explicitly whitelisted state file and loopback port. On a fresh PR204 checkout run:

    GRIT_PR192_DEMO_STATE=/tmp/grit-pr204-demo.state GRIT_PR192_DEMO_PORT=18766 bash "$DIR/deploy/run_line_printer_demo.sh" "$DIR"

Open with `ssh -L 18766:127.0.0.1:18766 root@141.105.66.249` and visit http://127.0.0.1:18766/demo. Old 18765 is not stopped/replaced. To stop ONLY new PR204 demo later:

    GRIT_PR192_DEMO_STATE=/tmp/grit-pr204-demo.state bash "$DIR/deploy/stop_line_printer_demo.sh"

Never stop an existing demo just to free the old state path. These are synthetic ephemeral containers/DB and do not access the production database. The standalone HTTP smoke runs during start; the browser UI is not yet validated until it is opened and exercised.
