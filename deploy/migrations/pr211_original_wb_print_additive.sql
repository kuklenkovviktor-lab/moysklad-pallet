-- GRIT WMS original WB FBS printer: ADDITIVE MIGRATION PROPOSAL.
-- DO NOT EXECUTE on production without explicit authorization,
-- validated backup and rollback plan. NOT applied by PR211 tests.
--
-- This schema is a prepared production-target analogue of the isolated
-- synthetic_pr211_* prototype; it is NOT wired into production runtime.
-- Prerequisite: reviewed printer registry from PR209/PR210 and WB v11 tables.

BEGIN;
DO $pr211$
BEGIN
  IF to_regclass('public.wb_fbs_orders') IS NULL
     OR to_regclass('public.wb_fbs_stickers') IS NULL
     OR to_regclass('public.fbs_catalog_barcodes') IS NULL
     OR to_regclass('public.grit_line_printers') IS NULL
  THEN
    RAISE EXCEPTION 'PR211 prerequisites missing; refusing migration';
  END IF;
END
$pr211$;

CREATE TABLE IF NOT EXISTS grit_wb_original_print_jobs (
    job_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    printer_id TEXT NOT NULL,
    line TEXT NOT NULL,
    sticker_hash TEXT NOT NULL,
    source_sku TEXT NOT NULL,
    source_product_id TEXT NOT NULL,
    source_article TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    encrypted_original_bitmap BYTEA,
    state TEXT NOT NULL DEFAULT 'STAGED'
      CHECK (state IN ('STAGED','QUEUED','CLAIMED','SUBMITTED','UNKNOWN','BLOCKED')),
    ticket_id TEXT UNIQUE,
    claimed_device_hash TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id,order_id)
);
CREATE INDEX IF NOT EXISTS grit_wb_original_print_next
  ON grit_wb_original_print_jobs(printer_id,state,created_at);

CREATE TABLE IF NOT EXISTS grit_wb_print_devices (
    device_id TEXT PRIMARY KEY,
    printer_id TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS grit_wb_print_devices_active
  ON grit_wb_print_devices(printer_id) WHERE enabled=TRUE;

CREATE TABLE IF NOT EXISTS grit_wb_original_print_events (
    event_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES grit_wb_original_print_jobs(job_id),
    event TEXT NOT NULL,
    actor_hash TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS grit_wb_original_print_events_time
  ON grit_wb_original_print_events(created_at DESC);

COMMIT;
