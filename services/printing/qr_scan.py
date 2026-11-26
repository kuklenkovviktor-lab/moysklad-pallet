"""Synthetic QR -> server-side automatic print enqueue. NO production routes.

This service ONLY accepts an employee/shift from a trusted server session and
one printer QR. Candidates come from the isolated PostgreSQL WB FBS replica.
Current QR, badge, shift, active picking, order, catalog, location and sticker
are revalidated inside the ENQUEUE transaction. No physical printing occurs.

Do NOT expose as a live endpoint, or trust employee_id/shift_id from HTTP JSON.
"""
from dataclasses import dataclass
import re

from line_printer_pg_integrated_trial import validate_locked_synthetic
from line_printer_pg_trial import _verify_isolated_connection
from line_printer_registry import Printer, verify_printer_scan
from services.printing.pg_queue import enqueue_synthetic_print_pg

import json

_PRINTER_QR = re.compile(r"^GRIT-PRINTER:1:([A-Za-z0-9_-]{1,40}):[A-Za-z0-9_-]{40,80}$")
_ACCOUNT = "synthetic"
_KIND = "WB_FBS"


STICKER_DDL = """CREATE TABLE IF NOT EXISTS synthetic_pr192_print_stickers (
 account_id text NOT NULL, order_id text NOT NULL,
 original_sticker text NOT NULL CHECK (length(original_sticker) > 0),
 PRIMARY KEY (account_id,order_id)
)"""


@dataclass(frozen=True)
class ScanResult:
    accepted: bool
    reason: str
    printer_id: str = ""
    added: int = 0
    already_queued: int = 0
    elsewhere: int = 0
    requires_review: int = 0


def initialize_synthetic_stickers(conn):
    """DDL for fabricated-only stickers, never a production WB label table."""
    _verify_isolated_connection(conn)
    with conn.transaction():
        conn.execute(STICKER_DDL)


def _current_printer(conn, printer_id, payload):
    """Read current server-side config, not line details in QR payload."""
    record = conn.execute("""SELECT name,lines_json,token_digest,enabled
          FROM grit_line_printers WHERE printer_id=%s FOR SHARE""",
                          (printer_id,)).fetchone()
    if not record:
        return None
    try:
        printer = Printer(printer_id, record[0], tuple(json.loads(record[1])),
                          record[2], bool(record[3]))
    except (TypeError, ValueError, KeyError):
        return None
    return printer if verify_printer_scan(payload, printer) else None


def _authorized_identity(conn, employee_id, shift_id):
    emp = conn.execute("SELECT active FROM employees WHERE id=%s FOR SHARE",
                       (employee_id,)).fetchone()
    if not emp or emp[0] != 1:
        return False
    shift = conn.execute("""SELECT employee_id,ended_at FROM shifts
                            WHERE id=%s FOR SHARE""",
                         (shift_id,)).fetchone()
    if not shift or shift[0] != employee_id or shift[1] is not None:
        return False
    activity = conn.execute("""SELECT 1 FROM activities
        WHERE employee_id=%s AND shift_id=%s AND activity_type='picking'
        AND ended_at IS NULL FOR SHARE""", (employee_id, shift_id)).fetchone()
    return bool(activity)


def scan_synthetic_printer_qr(connection_factory, *, qr_payload,
                              employee_id, shift_id):
    """One scan queues ALL eligible local orders for this printer.

    UI must provide only QR, not order IDs, account, user identity or lines.
    Security checks repeat under locks in the queue transaction to eliminate
    scan-vs-revoke and scan-vs-cancel windows. The returned counts NEVER imply
    successful physical printing.
    """
    if not all(isinstance(x, str) and 0 < len(x.strip()) <= 160
               for x in (qr_payload, employee_id, shift_id)):
        return ScanResult(False, "INVALID_INPUT")
    parsed = _PRINTER_QR.fullmatch(qr_payload)
    if not parsed:
        return ScanResult(False, "INVALID_QR")
    printer_id = parsed.group(1)

    # Initial read is a HINT only. Authorization is repeated in the
    # enqueue transaction before any inserts.
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            if not _authorized_identity(conn, employee_id, shift_id):
                return ScanResult(False, "TERMINAL_NOT_ACTIVE")
            if not _current_printer(conn, printer_id, qr_payload):
                return ScanResult(False, "QR_REVOKED_OR_PRINTER_DISABLED")
            candidates = conn.execute("""SELECT o.order_id
                FROM wb_fbs_orders o
                JOIN wb_fbs_picking_queue q
                   ON q.account_id=o.account_id AND q.order_id=o.order_id
                WHERE o.account_id=%s AND o.wb_status='confirm'
                  AND o.local_status='pending' ORDER BY o.order_id""",
                (_ACCOUNT,)).fetchall()

    def authorized_locked(conn, current_printer, actor_id):
        if current_printer != printer_id or actor_id != employee_id:
            return False
        return (_authorized_identity(conn, employee_id, shift_id) and
                _current_printer(conn, printer_id, qr_payload) is not None)

    def eligible_locked(conn, account_id, order_id, kind, current_printer):
        if (account_id != _ACCOUNT or kind != _KIND or
                current_printer != printer_id):
            return False
        # Fail closed if an original sticker has not actually been imported.
        # Synthetic fixture never generates a substitute WB sticker.
        sticker = conn.execute("""SELECT original_sticker
            FROM synthetic_pr192_print_stickers
            WHERE account_id=%s AND order_id=%s FOR SHARE""",
            (account_id, order_id)).fetchone()
        if not sticker or not sticker[0].strip():
            return False
        return validate_locked_synthetic(
            conn, account_id=account_id, order_id=order_id,
            employee_id=employee_id, shift_id=shift_id,
            printer_id=printer_id, qr_payload=qr_payload) == "ELIGIBLE"

    result = enqueue_synthetic_print_pg(
        connection_factory, printer_id=printer_id, actor_id=employee_id,
        candidates=[(_ACCOUNT, row[0], _KIND) for row in candidates],
        authorized_locked=authorized_locked,
        eligible_locked=eligible_locked)
    return ScanResult(
        result.accepted, result.reason, printer_id,
        result.added, result.existing, result.elsewhere, result.review)
