# GRIT WMS: QR printer autoqueue with real existing WMS auth in isolated fixture

Date: 26 Nov 2026. Issue #192; parent draft PR #200.

## Goal

Before changing the existing worker picking UI, prove that the **existing**
wms.get_session_employee() correctly authenticates a synthetic QR->print
queue request. No custom worker login or extra worker management screen.

## Changes

- tests/printing/test_qr_real_wms_auth.py: 14 HTTP integration tests.
- services/printing/qr_http_synthetic.py:
  make_real_wms_synthetic_autoqueue_blueprint() now verifies explicit
  synthetic feature flag and the actual isolated PG database connection
  BEFORE importing wms.py. The regular synthetic blueprint was already
  protected by the same gate.
- deploy/verify_line_printer_print_queue_pg_isolated.sh: launches the
  existing 16+21 PG17 tests and the 14 new real-WMS-auth tests against
  the same disposable PG17 Docker network (each test module cleans its
  own relevant fixture rows).

## Strict separation

Real wms.get_session_employee() is imported into a test-only Flask app
with an invented, temporary SQLite WMS database. It verifies the badge,
employee_id and bound shift, denying closed shifts and inactive employees.
The PostgreSQL test replica independently verifies picking activity, QR,
order status, catalog, shelf line and original synthetic sticker presence
before accepting an ENQUEUE transaction. This is deliberately a DOUBLE
synthetic identity check while real WMS session storage is SQLite-based.

The only HTTP POST body accepted by the test endpoint is QR + CSRF.
No order_id, line ID, account ID, employee ID or shift ID is trusted
from the client. Queue counts are NOT physical printer completion counts.

## Test procedure on HOSTKEY

Clone branch feature/pr192-qr-real-wms-auth-trial under a freshly created
/tmp/grit-pr201-* directory and run:

    chmod 755 "$DIR"
    bash "$DIR/deploy/verify_line_printer_print_queue_pg_isolated.sh" "$DIR"

Must end with:

    PR192 QR AUTOQUEUE REAL WMS AUTH ISOLATED PASS

This runner uses only a freshly initialized postgres:17 Docker container
with a tmpfs DB under an internal network; no host ports, production
Compose or WB API. No physical USB print commands.

**At PR creation:** 14 new real-WMS-auth tests have NOT run yet. Previous
PR #200 QR HTTP tests: 21/21 PASS on HOSTKEY, with 16/16 PG17 and
17/17 SQLite in earlier stages. No claim of full E2E completion.

## Remaining blockers

- Wire scanner QR detection into original picking screen *only under
  OFF-by-default feature flag* with full browser tests, after discussing
  existing scanner JS and current GRIT session cookie/CSRF routing.
- Verify real original WB barcode format and current printing eligibility.
- Design durable and safe print-agent delivery to virtual sink before USB.
- Handle unknown print outcomes and manager-assisted reprint.
- Decide policy of many thousands of orders per scan, batch loading.
- Separate production database schema review, rollback and security review.
- Never enable live production flow or merge drafts automatically.
