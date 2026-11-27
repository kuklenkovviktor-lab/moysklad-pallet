"""Prepare exactly ONE *physical* SYNTHETIC QR→USB approval in isolated demo.

Run ONLY inside the already validated PR210 Docker demo container via
deploy/Run-GritUsbTest.ps1 -Mode PhysicalOne. Do not execute on production.
No manager password, browser, QR scanner, WB API or live business data.

A stand-scoped permanent PG marker is written BEFORE generating a printer QR.
An uncertain/crashed attempt stays blocked; no unattended retries.
"""
import json
import os
import re
import sys
from pathlib import Path

# Executed as /app/deploy/*.py by docker exec. Resolve the existing
# mounted /app source without depending on PYTHONPATH set by the container.
ROOT = Path(__file__).resolve().parents[1]
if ROOT != Path("/app"):
    raise RuntimeError("ONLY_ISOLATED_DOCKER_APP_PATH_ALLOWED")
sys.path.insert(0, str(ROOT))

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_registry import create_printer
from line_printer_routing import route_order_to_printer
from services.printing.usb_test_preparation import stand_identity


def main():
    if (os.environ.get("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED"
            or os.environ.get("GRIT_PR210_AUTO_USB_SYNTHETIC") != "YES_EXPLICIT"
            or not os.environ.get("PR192_SYNTHETIC_DATABASE_URL", "").startswith(
                "postgresql://postgres:synthetic-only@pr192-db:5432/grit_pr192_synthetic_")
            or os.environ.get("DATABASE_URL", "").strip()):
        raise RuntimeError("ONLY_ISOLATED_PR210_DEMO_ALLOWED")

    printer_id = os.environ.get("GRIT_PR210_USB_PRINTER_ID", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", printer_id):
        raise RuntimeError("NO_BOUND_SYNTHETIC_PRINTER")

    # No test endpoint, application auth or printer driver outside demo app.
    # Freshly imported app's app.test_client invokes existing, real GRIT
    # authenticated worker handler + synthetic QR HTTP controller.
    os.environ["GRIT_PR210_PHYSICAL_ONE_SHOT"] = "YES_ISOLATED_ONE"
    import line_printer_demo_app as demo

    with demo.pg() as conn:
        _verify_isolated_connection(conn)
        stand_id = stand_identity(conn)
        with conn.transaction():
            conn.execute("""CREATE TABLE IF NOT EXISTS synthetic_pr210_physical_one_runs (
                stand_id text PRIMARY KEY,
                printer_id text NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                result text NOT NULL CHECK(result IN ('STARTED'))
            )""")
            # Prevent claims from pre-existing auto-approvals that the new
            # Windows single-shot mode could mistakenly consume.
            unresolved = conn.execute("""SELECT COUNT(*)
                FROM synthetic_pr192_usb_approvals a
                JOIN synthetic_pr192_print_events e ON e.job_id=a.job_id
                WHERE e.event='AUTO_USB_APPROVAL'
                AND a.phase IN ('APPROVED','CLAIMED')""").fetchone()[0]
            if unresolved:
                raise RuntimeError("UNRESOLVED_AUTO_TICKETS_REVIEW_REQUIRED")
            # Durable, fail-closed, one attempt per stand. Even after a
            # crash or uncertain spooler state we must NOT create another.
            new = conn.execute("""INSERT INTO synthetic_pr210_physical_one_runs
                 (stand_id,printer_id,result) VALUES (%s,%s,'STARTED')
                 ON CONFLICT DO NOTHING RETURNING stand_id""",
                 (stand_id, printer_id)).fetchone()
            if not new:
                raise RuntimeError("PHYSICAL_ONE_SHOT_ALREADY_STARTED_NO_RETRY")
            printer = conn.execute("""SELECT name,lines_json,enabled FROM grit_line_printers
                 WHERE printer_id=%s FOR UPDATE""", (printer_id,)).fetchone()
            if not printer or int(printer[2]) != 1:
                raise RuntimeError("BOUND_PRINTER_MISSING_OR_DISABLED")
            name, lines_json, _ = printer
            lines = tuple(json.loads(lines_json))
            # Create exactly one NEW invented order for the physical check.
            # It has a real catalogue+cell mapping within the isolated demo,
            # but no actual WB source label or customer data.
            candidates = conn.execute("""SELECT c.barcode,c.product_id,l.label,l.active
                 FROM fbs_catalog_barcodes c
                 JOIN cell_sku_assignments a ON a.product_id=c.product_id
                 JOIN replenishment_locations l ON l.id=a.location_id
                 WHERE c.is_pack=0 AND c.quantity=1
                 ORDER BY c.barcode,l.label""").fetchall()
            mapped = None
            for sku, product_id, label, active in candidates:
                assessment = route_order_to_printer(
                    dict(order_id="SYNTH-PR210-PHYSICAL-ONE",
                         wb_status="confirm", local_status="pending",
                         products=[product_id]),
                    dict(printer_id=printer_id,enabled=True,lines=lines),
                    [dict(product_id=product_id,label=label,
                          active=active == 1,confirmed=True)])
                if assessment.accepted:
                    mapped = sku
                    break
            if not mapped:
                raise RuntimeError("NO_SYNTHETIC_PRODUCT_IN_BOUND_PRINTER_LINES")
            order_id = "SYNTH-PR210-ONE-" + stand_id[:16]
            conn.execute("""INSERT INTO wb_fbs_orders
                 (account_id,order_id,wb_status,local_status)
                 VALUES('synthetic',%s,'confirm','pending')""", (order_id,))
            conn.execute("""INSERT INTO wb_fbs_picking_queue
                 (account_id,order_id) VALUES('synthetic',%s)""", (order_id,))
            conn.execute("""INSERT INTO wb_fbs_order_skus
                 (account_id,order_id,sku) VALUES('synthetic',%s,%s)""",
                 (order_id,mapped))
            conn.execute("""INSERT INTO synthetic_pr192_print_stickers
                 (account_id,order_id,original_sticker)
                 VALUES('synthetic',%s,%s)""",
                 (order_id,"SYNTHETIC-NOT-WB-"+stand_id[:16]))
            rotated, qr = create_printer(printer_id, name, lines)
            conn.execute("""UPDATE grit_line_printers SET token_digest=%s
                  WHERE printer_id=%s""", (rotated.token_digest, printer_id))

    with demo.app.test_client() as client:
        login = client.post("/demo/login",
                            json={"mode":"employee", "badge":"DEMO-BADGE-1"})
        if login.status_code != 200 or login.json.get("employee") != "demo-e1":
            raise RuntimeError("SYNTHETIC_EMPLOYEE_AUTH_FAILED")
        worker = client.get("/synthetic/pr192/print-queue/session")
        if worker.status_code != 200 or worker.json.get("synthetic") is not True:
            raise RuntimeError("AUTHENTICATED_FBS_SESSION_REQUIRED")
        posted = client.post("/synthetic/pr192/print-queue/scan",
                             json={"qr": qr,"csrf":worker.json["csrf"]})
        if posted.status_code != 200 or posted.json.get("auto_usb_approved") != 1:
            raise RuntimeError("SYNTHETIC_QR_DID_NOT_APPROVE_EXACTLY_ONE")

    with demo.pg() as conn:
        pending = conn.execute("""SELECT COUNT(*)
             FROM synthetic_pr192_usb_approvals a
             JOIN synthetic_pr192_print_events e ON e.job_id=a.job_id
             WHERE e.event='AUTO_USB_APPROVAL' AND a.phase='APPROVED'
               AND a.printer_id=%s""", (printer_id,)).fetchone()[0]
    if pending != 1:
        raise RuntimeError("EXPECTED_EXACTLY_ONE_AUTO_USB_TICKET")
    print("PR210 PHYSICAL ONE PREPARED: EXACTLY 1 SYNTHETIC 58x40")
    print("EMPLOYEE QR HTTP + POSTGRESQL17 AUTO APPROVAL PASS")
    print("NO MANAGER, NO WEB BROWSER, NO SCANNER, NO REAL WB")
    print("PREPARATION DOES NOT PRINT; NO SECOND PREPARATION ALLOWED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Details contain no secrets; do not expose QR or raw credentials.
        print("PR210 ONE-SHOT PREP REFUSED:", str(exc), file=sys.stderr)
        sys.exit(1)
