# GRIT WMS — synthetic manager printing dashboard (phase 8)

Date: 2026-11-26. Issue #192. Parent PR #204.

## Purpose

After browser acceptance of QR->queue (first scan: 1 new, re-scan: 1 already present), and one manual virtual delivery SYNTH-1002 QUEUED->SUBMITTED, provide a manager-only interface to see the queue and explicitly send ONE fake job to the isolated virtual sink.

## FSD modules and boundaries

- services/printing/manager_ui_synthetic.py: isolated-only Blueprint with manager guard; GET /demo/printing, GET /demo/printing/state, GET JS asset, POST /demo/printing/virtual-deliver.
- frontend/pages/printing_manager_demo/index.html: dark GRIT management page, job states, audit log, one virtual delivery button, explicit no USB/physical printing.
- frontend/features/printing_queue/ui.js: update, render rows with textContent, synthetic manager CSRF, one-job dispatch by printer ID only.
- line_printer_demo_app.py: register manager-only blueprint after existing is_manager, initialize disposable receipt table, add demo link.
- tests/printing/test_manager_printing_demo.py: 15 PG17 and real-WMS session synthetic tests on isolated fixtures.
- deploy/smoke_line_printer_demo.py: HTTP smoke tests manager queue, manager-only dispatch, QUEUED->SUBMITTED and idempotency.

## No external/physical effects

Only an invented synthetic dataset under an isolated PostgreSQL17 container. No USB, Windows API, Xprinter, real WB account, production database, production app.py or production wms.py. Job can reach SUBMITTED through fake sink ACK, never PRINTED. Manager cannot pass order_id or delivery mode in POST body; only printer_id and CSRF, validated server-side.

## Known limitations

- Last 100 jobs/events shown. Numbers are based on those visible rows, not global totals for large datasets.
- Queue is not processed automatically: explicit manager click simulates exactly one job. This is a verification workflow, not a production print architecture.
- Stale pre-dispatch source enters CANCELLED; uncertain delivery enters UNKNOWN and needs manager review. No automatic reprint.
- Device authentication, spooler delivery, physical proof, concurrency at scale and operational UX remain pending.
- New 15 tests and updated HTTP demo smoke have NOT run at PR creation. Prior synthetic tests and previous manual browser flow passed on earlier PR versions.
- Existing demos at 18765 and 18766 are to remain untouched; the runner now explicitly accepts a separate /tmp/grit-pr205-demo.state at port 18767, if later needed.

## Isolated PG17 regression

    DIR=$(mktemp -d /tmp/grit-pr205-XXXXXX)
    git clone -q --depth 1 --branch feature/pr192-virtual-print-manager-demo https://github.com/kuklenkovviktor-lab/moysklad-pallet.git "$DIR"
    chmod 755 "$DIR"
    bash "$DIR/deploy/verify_line_printer_print_queue_pg_isolated.sh" "$DIR"

Only after PASS, for separate browser demo (fresh disposable DB):

    GRIT_PR192_DEMO_STATE=/tmp/grit-pr205-demo.state GRIT_PR192_DEMO_PORT=18767 bash "$DIR/deploy/run_line_printer_demo.sh" "$DIR"

Do not run the isolated standalone demo until the new PG17 tests pass. No merge or live deploy.