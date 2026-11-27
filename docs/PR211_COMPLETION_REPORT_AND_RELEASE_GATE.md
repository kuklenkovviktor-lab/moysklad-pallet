# GRIT WMS: PR211 completion report / isolated release gate

Date: 2026-11-27. **NOT APPROVED FOR LIVE USE.**
This file is a status report, not permission to import employer orders or print.

## Confirmed evidence (do not overstate)

- **PR210**: the operator observed one PHYSICAL synthetic Xprinter label
  with `GRIT WMS / USB DEMO ONLY / NOT WB`. Does not prove real WB print.
- **PR211 before the current increment**: the operator ran the disposable
  PG17 → original-PDF bitmap → unchanged PR210 RAW algorithm with FAKE
  Windows spooler. Terminal showed 12/12 successful PG17 tests and final
  `PR211: SYNTHETIC VIRTUAL RUN PASS. NO DEPLOY`.
- **New tests in this increment**: committed, but NOT YET executed on HOSTKEY.
  Do not publish a PASS until the full runner exits 0 and prints its final line.

## Implemented now (only on PR211 branch)

1. **Original PDF validation**: unchanged original 58×40 page rasterized as
   203dpi one-copy TSPL BITMAP. The existing GRIT FBS PDF parser rejects
   unknown pages and duplicate printed 11-digit WB stickers. Checked against
   WB v11 source order, pending/confirm, picking queue, catalog barcode,
   source article/product, one active cell and printer line.
2. **WB scanner/printed-code parity**: `wb_qr_codec.canonical_wb_sticker`
   maps a valid 9-character `*BASE64` QR, Russian HID `;` variant and
   AIM `]Q3` to its original 11-digit printed WB barcode FOR COMPARISON.
   Never modify the original PDF bitmap. Ambiguous canonical duplicates are
   rejected for the entire PDF.
3. **PostgreSQL 17 encrypted outbox**: AES-256-GCM original pages,
   source/payload hashes, one order/one original label, states
   `STAGED → QUEUED → CLAIMED → SUBMITTED/UNKNOWN` plus `BLOCKED`.
   Immutable history; transaction commits before exposing data;
   no lease auto-reset or retry; exact source SKU/article/product, current
   cell/line, sticker and current sync status rechecked before claim.
4. **Manager PDF + worker QR**: new HTTP blueprint in EXISTING GRIT app,
   conditional on three separate disposable synthetic gates. Existing
   manager session/CSRF uploads and stages original PDF pages without
   printing. Existing authenticated /fbs worker badge, attendance, current
   picking shift and signed revocable printer QR release only that line's
   staged jobs.
5. **Windows/device security**: per-device random bearer credential hashed
   in DB, server-owned printer mapping, revoke, claim ticket bound to the
   claiming device. Another device on the same printer cannot ACK it.
   Agent's original TSPL envelope is validated against exact 58×40 bitmap
   before passing it to the original PR210 one-write RAW spool routine,
   with a permanent one-attempt journal. Only FAKE Windows spooler tests
   are allowed; this code does NOT enable real WB physical printing.
6. **Operations**: manager status and audit metadata without raw WB
   stickers/tokens; ciphertext removed only after explicit manager
   retention operation for acknowledged/blocked old jobs. Queued,
   CLAIMED and UNKNOWN ciphertext is preserved.
7. **UI**: isolated-only dark printer QR panel appended to existing
   `/fbs` and PDF staging/queue panel appended to existing
   `/manager/fbs-pdf` screens. No new independent WMS home page.
8. **Migration proposal**: `deploy/migrations/pr211_original_wb_print_additive.sql`
   is additive, contains prerequisite checks, no DROP/TRUNCATE/stock writes,
   and is **NOT executed**. Real device records disabled by default.

## Safety / feature gate

The existing `app.py` calls `register_pr211_isolated(app)`, which returns
immediately with **no effects** by default. Test routes can register ONLY if:

- `GRIT_PR211_ORIGINAL_FLOW=YES_ISOLATED_SYNTHETIC`
- `GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED`
- exact `PR211_SYNTHETIC_DATABASE_URL` pointing to disposable
  `postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_...`
- a separate valid 64-hex-digit `PR211_TEST_AES_KEY_HEX`

Any other configuration refuses startup rather than enabling printing.
Do NOT set these in production.

## Single isolated acceptance command on HOSTKEY

```bash
DIR=$(mktemp -d /tmp/grit-pr211-XXXXXX)
git clone -q --depth 1 --branch feature/pr211-wb-real-source-preflight \
  https://github.com/kuklenkovviktor-lab/moysklad-pallet.git "$DIR"
bash "$DIR/deploy/verify_pr211_full_pg17_pr210_virtual.sh" "$DIR"
```

Uses internal Docker network and ephemeral PostgreSQL17 in tmpfs; tests all
offline PDF contracts, 9char scanner normalization, release gates, existing
PR211 PG17 tests and manager→worker QR→authenticated device HTTP→PR210 RAW
fake Windows spooler with no real USB. The runner removes test DB/network
on exit. It does not deploy, connect to WB or mutate production. Successful
exit and final PASS line are required; a prior 12/12 PASS is NOT evidence of
these newly added tests.

## Still not complete / explicit release blockers

- **WB API availability**: the strict workflow requires verified rows in
  `wb_fbs_orders`, `wb_fbs_stickers` and picking queue. Importing a seller
  PDF alone into `pdf_fbs_orders` is not proof of actual WB status and
  MUST NOT generate a fake `confirm` order. If operating without WB API,
  a distinct explicitly approved PDF-only trust model and acceptance
  criteria are necessary before enabling real print.
- **PDF format variants**: source PDF parser is verified only for strict
  product-sheet + 11 printed decimal sticker pages. Scanner 9char decode
  does NOT magically make unsupported source PDF formats safe. Need
  representative synthetic samples of new source formats, page layout
  verification, and scan-readback on a permitted device.
- **Live Windows transport**: HTTP blueprint/agent only tested on synthetic
  PG17 plus injected fake Windows spooler. PR210 physical test was a
  NOT WB diagnostic. Production credential provisioning, certificate/
  endpoint policy, Windows DPAPI secret and printer mapping and actual
  original-WB print must pass separately in an explicitly permitted
  environment. No current authorization for employer warehouse testing.
- **Source and key lifecycle**: crypto-key rotation/retention must be
  operationally proven; never invalidate active encrypted UNKNOWN tickets.
- **Performance**: upload currently limited to 25 MiB; one atomic staging
  batch limited to 100 stickers. Larger batches need synthetic load testing
  and bounded chunk handling rather than raising limits blindly.
- **Release dependency**: PR210 is based on PR209, while PR211 is based on
  main. Integrate only after PR209/210 review, conflict resolution and
  reproducible end-to-end suite. Never assume copying one spool function
  has merged the entire Windows agent.
- **Backup**: a verified off-host backup and restore rehearsal for the
  target environment is not confirmed.

## Safe eventual release sequence (NO ACTION TAKEN)

1. Approve the real data source/trust model and original PDF formats.
2. Consolidate reviewed PR209/210/211 commits into one integration branch.
3. Complete isolated PG17 + authenticated HTTP + fake spooler load/security
   tests; record artifacts and exact SHA.
4. Verify backups and **restore into a separate isolated database**.
5. Review migration and execute ONLY after explicit owner authorization.
   Default devices remain disabled, original printing feature flag OFF.
6. Enable in an independently authorized test environment, never at the
   employer warehouse without revised permissions.
7. Rollback: disable feature and Windows agent; retain encrypted unresolved
   queue and audit; revert app-only release to verified previous image.
   Do not drop queue/audit tables or auto-requeue UNKNOWN tickets.

**Current operational recommendation: keep PR211 DRAFT; no merge to main,
no production deployment, no background jobs and no real WB printing.**
