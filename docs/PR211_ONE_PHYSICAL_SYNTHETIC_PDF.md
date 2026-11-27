# PR211: one physical ORIGINAL-LIKE SYNTHETIC PDF acceptance

**ONLY ONE 58×40 synthetic label on dedicated ONIX-TEST-XP460B.**
No real Wildberries PDF, orders, employees, credentials, printer or WMS DB.

## What the one-click Windows launcher actually does

`GRIT_PR211_PRINT_SYNTHETIC_PDF_ONCE.cmd` (supplied alongside the chat
as a downloadable batch file) fetches the latest PR211 branch to a Windows
temporary directory and launches `deploy/pr211_physical_usb_client.py`.

The Python client verifies:
- the exact **non-default** Windows spooler name `ONIX-TEST-XP460B`;
- a direct `USB###` port using unchanged PR210 `check_printer`;
- no previous permanent local attempt journal;
- a single SSH `grit` connection to HOSTKEY.

Via that connection, a fixed remote script:
- clones PR211 into `/tmp/grit-pr211-physical-*`;
- creates isolated internal Docker network and disposable PG17 with tmpfs;
- launches an existing GRIT image with read-only source volume;
- generates a 2-page **entirely invented** PDF: one product sheet and one
  strict 58×40 WB-like sticker page with a real vector Code128 for invented
  `59298125228` and visible raster `GRIT SYNTHETIC / NOT WB` watermark;
- parses the PDF with the *real* `fbs_pdf_labels.parse_wb_labels` (no mocks);
- passes the PDF through PR211 **manager upload** HTTP route;
- enrolls a server-bound synthetic Windows device token;
- verifies no ticket can be claimed BEFORE worker QR;
- simulates an authenticated synthetic picker/active shift scan with signed
  line-A printer QR, then obtains exactly one PostgreSQL17-backed encrypted
  original page via the authenticated device HTTP claim;
- sends only the verified **synthetic** bitmap and its hash/ticket over
  a private SSH stdout pipe to Windows (no public print endpoint);
- waits for one signed device ACK; deletes ephemeral Docker PG/network.

On Windows, PR211 validates original bitmap envelope and SHA-256, writes
a fixed **permanent one-shot journal BEFORE RAW write**, uses unchanged PR210
`_send_exactly_one`, and reports `SUBMITTED` or `UNKNOWN` through SSH.
No timer, spooler fallback, background loop, automatic retry, working printer
or production environment.

## Expected physical output

A single 58×40 mm label showing:
- `WB 59298125228` (invented synthetic 11-digit example)
- vector Code128 bars
- large raster watermark `GRIT SYNTHETIC`
- `NOT WB / TEST ONLY`

A successful Windows spooler ACK is **NOT proof of physical paper**. The
operator must directly observe the paper and scan-readback, if needed.

## Safety/caveats

- Previous PR210 physical print confirmed only diagnostic `NOT WB`.
- Previous PR211 full PG17 virtual run confirmed 12/12 on older commit.
- New physical PDF route and added tests **have not yet been executed on
  the operator's HOSTKEY or Xprinter in this turn**. Do not claim PASS until
  operator observes the paper and sends logs.
- The strict reader recognizes the `WB <11 digits>` PDF format. This
  acceptance does NOT validate all original WB PDF variants.
- This is an isolated script session using the existing HTTP handler logic
  through Flask's test client, **not** a public production HTTP service.
- Production source verification requires actual WB v11 source records;
  **PDF-only sites without an authorized WB API connection are not ready**.
- Re-running a failed/uncertain physical attempt is unsafe. Read the Windows
  journal/print queue; do not delete it to force another print.
- No release, migration execution, main merge or employer warehouse test.
