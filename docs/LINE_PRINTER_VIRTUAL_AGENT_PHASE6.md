# GRIT WMS — virtual print-agent (synthetic, no physical output)

Date: 2026-11-26. Parent draft PR #201, issue #192.

## Purpose

Extend the synthetic path: **existing picking session → printer QR →
server-side eligible order selection → PostgreSQL QUEUED job → virtual
agent → synthetic receipt + SUBMITTED**. Nothing touches Xprinter/USB,
production DB, WB API or real order labels.

FSD/backend package:
- services/printing/virtual_agent.py — virtual-only agent, dispatch
  prechecks, simulated sink and stale-SENDING reconciliation
- tests/printing/test_virtual_agent.py — 21 new integration scenarios
- deploy/verify_line_printer_print_queue_pg_isolated.sh — disposable PG17
  test runner extended with this suite

## Data flow

1. Claim only QUEUED jobs for the requested printer using
   SELECT ... FOR UPDATE SKIP LOCKED, ordered by creation.
2. Recheck CURRENT printer activation/configuration, local WB
   confirm/pending, picking queue membership, exact product barcodes,
   active confirmed mapped cells and synthetic original sticker while
   holding PostgreSQL locks. If stale, move QUEUED → CANCELLED and audit
   *before* any virtual sink interaction. Automatic re-enqueue of a
   CANCELLED job is not implemented.
3. If eligible, move QUEUED → SENDING and commit the claim.
4. Virtual sink stores a SHA-256 digest of its fabricated sticker in
   synthetic_pr192_virtual_receipts, unique by job_id. No raw sticker
   is logged or stored in the receipt.
5. An ACK causes SENDING → SUBMITTED. **Never PRINTED** because a
   virtual software ACK is not physical printer confirmation.

Failure simulation:
- CERTAIN_FAILURE: no sink call, SENDING → FAILED; manager review required
- UNKNOWN_AFTER_ACCEPT: receipt exists but ACK lost, SENDING → UNKNOWN
- CRASH_BEFORE_SINK: leaves SENDING without virtual receipt
- CRASH_AFTER_ACCEPT: leaves SENDING with receipt
- Explicit reconciliation of *stale* SENDING → UNKNOWN, never resends.
  New QR scans do not create duplicate jobs or re-send prior jobs.

## Limitations before real integration

- This is NOT a Windows print spooler, USB driver, or physical receipt.
- Agent ID strings are synthetic test actors, **not authenticated device
  credentials**. Never expose this module as a production HTTP route.
- No verified physical PRINTED state or real sticker rendering.
- No live manager reprint interface; UNKNOWN/FAILED remain blocked.
- One-job-per-call model; future agent loop needs rate limiting, queue
  backpressure, device authentication, batch processing and fair scheduling.
- Simulation uses synthetic PG fixtures, not a production-safe migration.
- Before real printer I/O: proof of original WB label bytes, schema review,
  explicit agent pairing, lease expiration, exactly-once/uncertainty handling,
  spooler result interpretation, operator approval and isolated hardware tests.
- No merge or deployment to production.

## Tests

Run on HOSTKEY against a disposable Docker network and PostgreSQL 17:
  bash "$DIR/deploy/verify_line_printer_print_queue_pg_isolated.sh" "$DIR"

Existing suites (reported PASS from prior PR):
  16 isolated PG queue + 21 QR HTTP + 14 real WMS auth.
New suite tests 21 virtual agent cases, with independent test fixtures and
fresh in-memory/temp synthetic labels. Expected final marker:
  PR192 VIRTUAL PRINT AGENT PG17 PASS

**The 21 new virtual-agent tests are not yet run at PR creation.**
All prior PASS claims belong to earlier branch versions only.
