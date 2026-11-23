# WB FBS Sync v1.1 (draft, not for production)

**Default:** OFF. `WB_FBS_V11_ENABLED=0`.

## Worker scan sequence
1. Employee enters via badge and starts FBS picking.
2. Employee scans merchandise barcode; existing server-side pending product is recorded.
3. Employee scans WB machine-readable sticker/QR.
4. Local database looks up `(account_id, sticker.barcode)`, then the linked WB order.
5. Verify order is in pickable status, cache is current, and product barcode belongs
   to the exact order variant SKUs. No matching by article alone.
6. On success, atomic FBS commit records scan and marks local WB order completed.
   On mismatch, attempt is logged, no scan count is added, and pending product is reset.
7. Retry of the same request ID is idempotent.

No WB HTTP request is made by the scan handler. Existing PDF mode is unchanged while OFF.

## Environment switches

- `WB_FBS_V11_ENABLED=1`: enable new route and initialize additive tables.
- `WB_FBS_V11_ACCOUNT_ID`: account namespace; default `default` (single seller).
- `WB_API_TOKEN`: marketplace token, server-side only.
- `WB_CONTENT_TOKEN`: optional separate Content API key, server-side only.
- `WB_FBS_MAX_STALE_SECONDS`: maximum age of cached order state, default 180.

**Never enable v1.1 without end-to-end tests, API schema verification, and documented owner approval.**

## Interfaces
- Manager: `/manager/wb-fbs-v11`; accessible through **Интеграции**.
- Status: `GET /api/wb-v11/status`, manager only.
- Trigger: `POST /api/wb-v11/sync`, manager session + CSRF.
- Existing terminal remains `/api/fbs/scan` and uses product-first pending state.

## Worker
`python -m wb_fbs_sync_v11` is the dedicated read-only scheduling process.
It must run **exactly once per seller**; never launch in each Gunicorn worker.
Cadences: new 30 s, statuses 60 s, catalog 6 h, full reconciliation 24 h.
The worker process is **not** included in current deployed Compose. Cross-process
coordination, credential provisioning and operational alerts require further review.

## Test
From an isolated HEAD checkout on HOSTKEY:

```bash
bash deploy/verify_wb_fbs_v11.sh
```

Tests run in an offline Docker container with ephemeral SQLite and synthetic secrets.
Do not mount production DB, compose volumes, employee badges, or live WB credentials.

## Release gate
- All synthetic and existing attendance/FBS regression tests pass.
- Confirm WB responses: status strings, status pagination, sticker barcode exactly
  as scanned by actual supported barcode/QR scanners, and sticker availability.
- Check PDF-to-API transition for existing unfinished orders.
- Verify no duplicate order ID/collision across seller accounts.
- Verify multi-process sync exclusivity and token refresh/monitoring.
- Test actual manager UX in an isolated environment, with only synthetic data.
- App-only deployment and rollback plan approved separately.

No changes to live PostgreSQL schema or warehouse data should occur during testing.


## Pre-merge audit, 2026-11-21

1. **WB API contract**: v1.1 endpoints and fields checked against published Marketplace FBS and Content API references. Exact QR scanner output versus API sticker.barcode still needs controlled validation; no real token was used.
2. **Cache freshness**: maximum age defaults to 1800 seconds; malformed timestamps and missing status entries reject scans. Old confirm is not reused if the status response omitted an order.
3. **Concurrent sync**: every sync entry point has a process lock plus PostgreSQL per-account advisory lock. Postgres cross-process tests on an isolated synthetic database are still required.
4. **Scan bypasses**: authenticated employee, active picking shift, server-held pending product, exact SKU, WB sticker and atomic revalidation required. No article-only fallback when v1.1 is enabled. Existing PDF path stays untouched when disabled.

New HTTP mock cases cover cancellation, status changes, sticker replacement, 401/403, 429, 5xx, timeout, cache retention, missing status and lock contention.

**Test gate not yet confirmed after these audit changes.** Run isolated Docker verification before merging. Physical scanner/QR identity is an explicit remaining release gate.

## Disposable PostgreSQL concurrency and QR gates

Use only an isolated checkout of PR 168 on HOSTKEY. The script refuses inherited DATABASE_URL and WB tokens, creates an internal Docker network and a fresh PostgreSQL 17 container. No production networks or database volumes are attached. Resources are removed by an EXIT trap.

    cd /root/grit-wb-v11-check
    git pull --ff-only origin feature/wb-fbs-sync-v11-20261121
    env -u DATABASE_URL -u WB_API_TOKEN -u WB_CONTENT_TOKEN bash deploy/verify_wb_fbs_pg_isolated.sh

The test creates 10,000 fake WB orders, 10 synthetic terminal shifts and 1,000 successful atomic commits. It measures elapsed/average/P95 latency, rejects concurrent double-counting, rejects wrong SKU and canceled orders, checks real PostgreSQL advisory locks between Python processes and asserts local lookup sends zero WB network traffic. This is a real PostgreSQL database test, but not a full mobile-device HTTP test.

Offline QR equality comparator test:

    python deploy/verify_wb_fbs_qr_contract.py --synthetic

The QR synthetic test proves only that our comparison rejects altered, empty or mismatched strings. A real QR/API match is NOT proved without authorized, non-employer paired samples of the machine-read sticker QR payload and the same order's API barcode field. Optional fixture JSON: samples array of {api_barcode, scanner_value}; run with --fixture <path> locally. The script never calls WB or prints actual order IDs.

Do not enable WB_FBS_V11_ENABLED=1 or declare real QR integration PASS until the authorized QR sample is independently verified.

## GRIT queue after WB changes from "На сборке" to "Доставка"

For the warehouse's workflow, moving the order from WB `supplierStatus=confirm`
to `supplierStatus=complete` does **not** mean that warehouse picking is finished.
GRIT keeps a durable `wb_fbs_picking_queue` entry, set only when an order was
actually observed as `confirm`, and a separate `wb_fbs_orders.local_status`
(`pending` or `complete`). New orders are appended without resetting previous
unassembled entries.

A merchandise barcode is accepted only if at least one matching SKU belongs to
an order already admitted to that queue, the order's current supplier status is
`confirm` or `complete`, and its local status is not `complete`. The second
scan still requires that the specific order's WB QR matches that exact SKU,
that the cached WB status is fresh, and that the order has not already been
completed in GRIT. Cancellations and other non-eligible statuses remain blocked.
The status worker keeps syncing accepted `complete` orders rather than aging
them out after 48 hours.

**Migration caveat:** pre-existing shipments for which no evidence of a previous
`confirm` status was retained cannot be automatically enrolled. The additive
queue starts empty and is populated as new `confirm` snapshots arrive. Do not
guess eligibility from an existing sticker or the fact that a record appears
in a 14-day historical WB import. Legacy delivery orders require a separately
authorized, audited reconciliation of actual picking eligibility.

Both **the app and the dedicated status worker** must run the updated
`wb_fbs_sync_v11.py`. Updating only the app would leave the old worker
coercing `complete` to `unknown` and not refreshing accepted deliveries.
Run isolated synthetic tests before releasing; do not change the live DB
or remove worker rollback bind mounts as part of a test.
