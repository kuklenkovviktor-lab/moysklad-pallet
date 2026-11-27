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

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_registry import create_printer
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
