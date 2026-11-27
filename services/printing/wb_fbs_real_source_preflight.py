"""Read-only adapter for REAL GRIT WB FBS tables and storage map.

This is deliberately NOT a production printer, HTTP endpoint, migration or
automatic dispatch. Existing WB sticker barcode is NOT proof of a printable
58x40 label. Never return sticker bytes to a client or synthesize WB artwork.
All reads can be run against disposable SQLite/PG test fixtures.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re


@dataclass(frozen=True)
class Candidate:
    account_id: str
    order_id: str
    decision: str
    line: str = ""
    product_id: str = ""
    # No sticker/barcode content, customer information or secrets in output.


def _line(label):
    m = re.match(r"^([А-ЯЁA-Z]{1,3})-?\d", str(label or "").strip().upper())
    return m.group(1) if m else ""


def _read(conn, statement, args=(), adapt=lambda value: value):
    cur = conn.cursor()
    try:
        cur.execute(adapt(statement), args)
        return cur.fetchall()
    finally:
        cur.close()


def select_wb_fbs_candidates(conn, *, printer_lines, max_orders=100,
                             max_sync_age_seconds=180,
                             clock=None, adapt=lambda value: value):
    """Inspect real production *schema*, without doing any printing/writes.

    printer_lines is a server-verified printer config, NEVER user HTTP input.
    The caller must separately authorize terminal/employee/shift/QR; this
    function is NOT an authorization boundary.

    A routable record is still NOT PRINTABLE: wb_fbs_stickers contains a
    barcode, not a verified source label document or original WB TSPL.
    """
    if (not isinstance(printer_lines, (tuple, list, frozenset))
            or not printer_lines
            or len(printer_lines) > 20
            or any(not isinstance(x, str) or not re.fullmatch(r"[А-ЯЁA-Z]{1,3}", x)
                   for x in printer_lines)):
        raise ValueError("SERVER_VERIFIED_PRINTER_LINES_REQUIRED")
    if type(max_orders) is not int or not 1 <= max_orders <= 1000:
        raise ValueError("INVALID_READ_LIMIT")
    if type(max_sync_age_seconds) is not int or not 30 <= max_sync_age_seconds <= 86400:
        raise ValueError("INVALID_STALE_LIMIT")
    clock = clock or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise ValueError("UTC_CLOCK_REQUIRED")

    rows = _read(conn, """
        SELECT o.account_id,o.order_id,o.wb_status,o.local_status,o.synced_at,
               s.barcode,s.format
        FROM wb_fbs_orders o
        JOIN wb_fbs_picking_queue q
          ON q.account_id=o.account_id AND q.order_id=o.order_id
        LEFT JOIN wb_fbs_stickers s
          ON s.account_id=o.account_id AND s.order_id=o.order_id
        ORDER BY o.account_id,o.order_id LIMIT ?
    """, (max_orders,), adapt)
    outcomes = []
    for account, order, wb_status, local_status, synced, sticker, fmt in rows:
        def refused(reason):
            outcomes.append(Candidate(str(account), str(order), reason))
        if wb_status != "confirm" or local_status != "pending":
            refused("STATUS_NOT_PICKABLE")
            continue
        try:
            last = datetime.fromisoformat(str(synced).replace("Z", "+00:00"))
            if last.tzinfo is None or (clock-last.astimezone(timezone.utc)).total_seconds() > max_sync_age_seconds or last > clock:
                refused("STALE_OR_FUTURE_WB_SNAPSHOT")
                continue
        except (ValueError, TypeError, OverflowError):
            refused("BAD_WB_SNAPSHOT_TIMESTAMP")
            continue
        if not isinstance(sticker, str) or not sticker.strip():
            refused("ORIGINAL_STICKER_ABSENT")
            continue

        # A seller SKU/barcode is resolved ONLY through the persisted GRIT
        # catalogue. Bundles, non-unit packs, missing and ambiguous maps fail.
        sku_rows = _read(conn, """SELECT sku FROM wb_fbs_order_skus
            WHERE account_id=? AND order_id=? ORDER BY sku""", (account, order), adapt)
        if not sku_rows:
            refused("SKU_MISSING")
            continue
        products = set()
        problem = None
        for (sku,) in sku_rows:
            matches = _read(conn, """SELECT product_id,quantity,is_pack
                FROM fbs_catalog_barcodes WHERE barcode=?""", (sku,), adapt)
            if len(matches) != 1:
                problem = "SKU_NOT_UNIQUELY_MAPPED"
                break
            product_id, qty, is_pack = matches[0]
            if not product_id or float(qty) != 1.0 or int(is_pack) != 0:
                problem = "PACK_OR_INVALID_UNIT"
                break
            products.add(str(product_id))
        if problem:
            refused(problem)
            continue
        if len(products) != 1:
            refused("MULTI_PRODUCT_ORDER_REQUIRES_REVIEW")
            continue
        (product,) = tuple(products)
        assignments = _read(conn, """SELECT l.label,l.active
            FROM cell_sku_assignments a
            JOIN replenishment_locations l ON l.id=a.location_id
            WHERE a.product_id=? ORDER BY l.id""", (product,), adapt)
        if len(assignments) != 1 or int(assignments[0][1]) != 1:
            refused("CELL_MISSING_AMBIGUOUS_OR_INACTIVE")
            continue
        line = _line(assignments[0][0])
        if not line or line not in printer_lines:
            refused("PRINTER_LINE_MISMATCH")
            continue
        # The format column is NOT a TSPL or document payload. No real label
        # may be printed until a separate validated renderer/source is built.
        outcomes.append(Candidate(str(account),str(order),
                                  "ROUTABLE_LABEL_SOURCE_UNVERIFIED",line,product))
    return tuple(outcomes)
