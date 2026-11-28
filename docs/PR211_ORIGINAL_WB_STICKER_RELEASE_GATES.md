# PR211 — release gates (maintained)

The current authoritative development and release checklist is:

**[PR211_COMPLETION_REPORT_AND_RELEASE_GATE.md](PR211_COMPLETION_REPORT_AND_RELEASE_GATE.md)**

Summary:
- PR210 operator-observed physical `GRIT WMS / USB DEMO ONLY / NOT WB` label: confirmed synthetic print.
- Earlier PR211 isolated PostgreSQL17 → original bitmap → actual PR210 RAW algorithm on a fake Windows spooler: operator confirmed **12/12 PASS**.
- Later PR211 changes implement manager PDF stage, worker signed printer QR release, 9-character WB QR↔11-digit PDF code comparison, scoped device authentication/revocation, same-device ACK, AES-GCM queue and retention, test-only dark widgets in existing app and additive migration proposal.
- **NEW regression/HTTP tests are committed but NOT YET EXECUTED on HOSTKEY**. The older 12/12 result cannot certify the updated branch.
- Existing `pdf_fbs_orders` registry contains mapping metadata but not original PDF bytes. New encrypted verified bitmap queue is distinct; avoid a second stock/order import.
- **No verified WB API source for PDF-only warehouses:** do not fabricate `confirm` status from a seller PDF. This remains a separate trust-model decision.
- All original-printing features are disabled by default. No main merge, no production deployment, no live WB data or employer hardware.

Single synthetic runner:

```bash
DIR=$(mktemp -d /tmp/grit-pr211-XXXXXX)
git clone -q --depth 1 --branch feature/pr211-wb-real-source-preflight \
  https://github.com/kuklenkovviktor-lab/moysklad-pallet.git "$DIR"
bash "$DIR/deploy/verify_pr211_full_pg17_pr210_virtual.sh" "$DIR"
```

Keep PR211 draft until the new isolated regressions pass and all release prerequisites are explicitly authorized.
