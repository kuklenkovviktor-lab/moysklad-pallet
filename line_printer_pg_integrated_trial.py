"""Synthetic-only, read/lock integration gate for GRIT printer routing (#192).

This module intentionally has NO live Flask route, no real print action and
no production schema migration. The adapter uses the *actual* existing GRIT
table/column names, seeded ONLY into a disposable isolated PG test database.

Pending review before production:
- mapping freshness and source-of-truth policy
- badge/session binding at the HTTP boundary
- idempotent replay after order/employee status changes
- real-order row locking interoperability and transactional lifecycle
"""
from hashlib import sha256
from hmac import compare_digest

from line_printer_pg_trial import reserve_synthetic_pg
from line_printer_routing import line_of, route_order_to_printer

def validate_locked_synthetic(conn, *, account_id, order_id, employee_id,
                              shift_id, printer_id, qr_payload):
    """Validate against locked synthetic copies of actual GRIT schema."""
    employee = conn.execute(
        "SELECT active FROM employees WHERE id=%s FOR SHARE", (employee_id,)
    ).fetchone()
    if not employee or employee[0] != 1:
        return "EMPLOYEE_INACTIVE"
    shift = conn.execute("""
        SELECT employee_id,ended_at FROM shifts
        WHERE id=%s FOR SHARE
    """, (shift_id,)).fetchone()
    if not shift or shift[0] != employee_id or shift[1] is not None:
        return "SHIFT_NOT_ACTIVE"
    activity = conn.execute("""
        SELECT 1 FROM activities
        WHERE shift_id=%s AND employee_id=%s
          AND activity_type='picking' AND ended_at IS NULL
        FOR SHARE
    """, (shift_id, employee_id)).fetchone()
    if not activity:
        return "PICKING_NOT_ACTIVE"

    record = conn.execute("""
        SELECT name, lines_json, token_digest, enabled
        FROM grit_line_printers WHERE printer_id=%s FOR SHARE
    """, (printer_id,)).fetchone()
    if not record or record[3] != 1:
        return "PRINTER_DISABLED"
    if not isinstance(qr_payload, str) or len(qr_payload) > 160:
        return "QR_INVALID"
    # Full signature format and printer ID are validated by the pure registry.
    from line_printer_registry import Printer, verify_printer_scan
    import json
    try:
        printer = Printer(printer_id, record[0], tuple(json.loads(record[1])),
                          record[2], bool(record[3]))
    except (TypeError, ValueError, KeyError):
        return "PRINTER_CONFIG_INVALID"
    if not verify_printer_scan(qr_payload, printer):
        return "QR_INVALID"

    order = conn.execute("""
        SELECT wb_status, local_status FROM wb_fbs_orders
        WHERE account_id=%s AND order_id=%s FOR UPDATE
    """, (account_id, order_id)).fetchone()
    if not order:
        return "ORDER_MISSING"
    queued = conn.execute("""
        SELECT 1 FROM wb_fbs_picking_queue
        WHERE account_id=%s AND order_id=%s FOR SHARE
    """, (account_id, order_id)).fetchone()
    if not queued:
        return "ORDER_NOT_IN_QUEUE"
    rows = conn.execute("""
        SELECT sku FROM wb_fbs_order_skus
        WHERE account_id=%s AND order_id=%s FOR SHARE
    """, (account_id, order_id)).fetchall()
    if not rows:
        return "ORDER_SKUS_MISSING"

    products = []
    for row in rows:
        sku = row[0]
        # Exact unit-barcode lookup; never guess product ID via seller article.
        catalog = conn.execute("""
            SELECT product_id FROM fbs_catalog_barcodes
            WHERE barcode=%s AND is_pack=0 AND quantity=1 FOR SHARE
        """, (sku,)).fetchone()
        if not catalog or not catalog[0]:
            return "CATALOG_UNIT_BARCODE_MISSING"
        products.append(catalog[0])

    # Locks confirmed mappings and their active parent locations for this
    # synthetic integration gate. Actual audit DB does not expose a separate
    # confirmed boolean, so a persisted mapping is the existing confirmation.
    mappings = conn.execute("""
        SELECT a.product_id, l.label, l.active
        FROM cell_sku_assignments a
        JOIN replenishment_locations l ON l.id=a.location_id
        WHERE a.product_id=ANY(%s::text[])
        FOR SHARE OF a,l
    """, (products,)).fetchall()
    locations = [
        {"product_id": item[0], "label": item[1],
         "active": item[2] == 1, "confirmed": True}
        for item in mappings
    ]
    decision = route_order_to_printer(
        {"order_id": order_id, "wb_status": order[0],
         "local_status": order[1], "products": products},
        {"printer_id": printer_id, "enabled": True, "lines": printer.lines},
        locations)
    return "ELIGIBLE" if decision.accepted else decision.reason


def reserve_integrated_synthetic(
    connection_factory, *, account_id, order_id, employee_id, shift_id,
    printer_id, qr_payload, request_id,
):
    """Atomic first-claim trial using trusted data reread under PG locks.

    This is not a live API and cannot be used against a non-isolated DB.
    """
    denial = {"reason": "NOT_CHECKED"}
    def validator(conn):
        reason = validate_locked_synthetic(
            conn, account_id=account_id, order_id=order_id,
            employee_id=employee_id, shift_id=shift_id,
            printer_id=printer_id, qr_payload=qr_payload)
        denial["reason"] = reason
        return reason == "ELIGIBLE"
    result = reserve_synthetic_pg(
        connection_factory, account_id=account_id, order_id=order_id,
        printer_id=printer_id, employee_id=employee_id,
        request_id=request_id, is_eligible_locked=validator,
        replay_authorizer_locked=validator)
    return result, (denial["reason"] if result.reason in
                    ("ORDER_INELIGIBLE", "REPLAY_DENIED_STALE_STATE")
                    else result.reason)
