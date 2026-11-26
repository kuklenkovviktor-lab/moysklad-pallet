# GRIT WMS: PostgreSQL 17 print queue, isolated contract (26.11.2026)

Parent: draft PR #197 (SQLite print queue), based on draft PR #193, issue #192.
This is **not** a live WMS change. No production routes, no live PostgreSQL
migration, no WB calls and no USB/Xprinter print action.

## Architecture

line_printer_pg_print_queue.py creates only synthetic_pr192_print_jobs and
synthetic_pr192_print_events in a disposable grit_pr192_synthetic_* PostgreSQL
database. It reuses the existing _verify_isolated_connection gate. Enqueue
checks a trusted auth callback and a per-order eligibility callback INSIDE
the same database transaction; it acquires transaction-scoped advisory locks
and uses UNIQUE(account_id,order_id,sticker_kind) as the final guard.

The candidate list must come from trusted server-side selection. It is not
valid to call these functions directly from client JSON. The eligibility
callback must check live status, QR, picking shift, printer lines, exact
unit SKU and usable ORIGINAL WB sticker while locks are held. The current
PR does not yet implement this complete adapter. Duplicate scan preserves
existing jobs, does NOT resubmit any spooler command.

Lifecycle:
- QUEUED -> SENDING -> SUBMITTED -> PRINTED, where PRINTED requires separate
  trustworthy physical evidence. A spooler acknowledgment is only SUBMITTED.
- SENDING -> FAILED only with reliable not-printed proof; uncertain outcomes
  transition to UNKNOWN, not to QUEUED.
- UNKNOWN/FAILED -> QUEUED requires a separately trusted authorization and
  reviewed-safe-to-retry evidence with reason. This prototype's evidence
  strings are placeholders, not trusted printer hardware signals.
- PRINTED and CANCELLED are terminal. Repeat label printing is NOT implemented.

Each accepted state change writes an immutable synthetic audit event with
a globally unique action_id. The transaction locks both the action ID and
the row to prevent duplicate actions.

## Tests

File: tests/test_line_printer_pg_print_queue.py (16 test methods).
Cases: initial scan, duplicate scan, new arrivals on rescanning,
8-terminal PostgreSQL concurrency, isolation by account, deny auth,
deny eligibility, atomic rollback, evidence for printed/failed, unknown,
manual review, audit and action_id, cancelled terminal, fail-closed gate.

Isolated HOSTKEY runner:
  bash /path/to/checkout/deploy/verify_line_printer_print_queue_pg_isolated.sh /path/to/checkout

It creates its own internal Docker network and a new postgres:17 tmpfs DB,
binds no host port, uses the existing GRIT image with psycopg installed, and
cleans only its own container/network after the run. It does NOT use the live
Docker Compose configuration or any production secret.

**Verification status as of creation**: integration test source prepared;
PG17 Docker test has NOT been executed in this session. Therefore no
"PASS" or performance assurance is claimed for the new PostgreSQL code.
The previous SQLite tests from PR #197 are a different test scope.

## Release blockers

1. Run the isolated PG17 runner and fix any issues. Repeat race tests.
2. Implement server-only authenticated QR adapter within current picking UI
   with present attendance and badge, and deny forged JSON identities.
3. Filter current local WB queue with one authoritative status and
   line/stock/catalog snapshot, including exact original sticker. Recheck
   immediately before agent dispatch in the same transaction.
4. Add manager-only queue monitoring and explicit reprint/exception review.
5. Design isolated Windows/USB virtual print agent, safe receipt protocol,
   uncertain outcomes and persistent delivery attempts. Never count OS
   spool acknowledgment as physical print proof.
6. End-to-end synthetic UI/HTTP/PG tests, printer simulation, negative
   tests, resource limits and recovery/load tests before release review.

The demo UI from #193 with manual reserve/scan/complete is NOT the desired
employee UX. No separate worker management page is planned.
