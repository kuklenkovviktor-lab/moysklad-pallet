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
