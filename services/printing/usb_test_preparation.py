"""Atomic preparation of one invented USB attempt, isolated PG17 only."""
import hashlib
import json
import re
import uuid

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_routing import route_order_to_printer
from services.printing.virtual_agent import _validate_before_dispatch


def stand_identity(conn):
    name = _verify_isolated_connection(conn)
    version = int(conn.execute("SHOW server_version_num").fetchone()[0])
    if version // 10000 != 17:
        raise ValueError("POSTGRESQL_17_REQUIRED")
    return hashlib.sha256(name.encode()).hexdigest()


def prepare_locked(conn, printer_id, request_id, manager_id):
    """Caller must verify manager session + CSRF. Everything commits together."""
    stand = stand_identity(conn)
    if not re.fullmatch(r"[0-9a-f]{32}", request_id):
        raise ValueError("INVALID_REQUEST_ID")
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('GRIT-USB-ONE-APPROVAL',0))")
    previous = conn.execute("""SELECT a.printer_id,a.phase,j.state,j.order_id
        FROM synthetic_pr192_usb_approvals a JOIN synthetic_pr192_print_jobs j USING(job_id)
        WHERE a.ticket_id=%s""", (request_id,)).fetchone()
    if previous:
        if previous[0] != printer_id:
            raise ValueError("REQUEST_ID_REUSED")
        return dict(state="READY" if previous[1:3] == ("APPROVED", "QUEUED") else
                    ("SUBMITTED_TO_WINDOWS" if previous[2] == "SUBMITTED" else "UNKNOWN"),
                    ticket_id=request_id, stand_id=stand, order_id=previous[3])
    if conn.execute("SELECT 1 FROM synthetic_pr192_usb_approvals WHERE phase IN ('APPROVED','CLAIMED')").fetchone():
        raise ValueError("ACTIVE_ATTEMPT_EXISTS")
    if conn.execute("SELECT 1 FROM wb_fbs_orders WHERE account_id <> 'synthetic' LIMIT 1").fetchone():
        raise ValueError("NON_SYNTHETIC_DATA_REFUSED")
    printer = conn.execute("SELECT lines_json,enabled FROM grit_line_printers WHERE printer_id=%s FOR SHARE",
                           (printer_id,)).fetchone()
    if not printer or printer[1] != 1:
        raise ValueError("PRINTER_MISSING_OR_DISABLED")
    lines = json.loads(printer[0])
    order_id = "SYNTH-USB-" + request_id
    selected = None
    for sku, product in conn.execute("""SELECT barcode,product_id FROM fbs_catalog_barcodes
        WHERE is_pack=0 AND quantity=1 ORDER BY barcode FOR SHARE""").fetchall():
        rows = conn.execute("""SELECT l.label,l.active FROM cell_sku_assignments a
            JOIN replenishment_locations l ON l.id=a.location_id WHERE a.product_id=%s
            FOR SHARE OF a,l""", (product,)).fetchall()
        decision = route_order_to_printer(
            dict(order_id=order_id, wb_status="confirm", local_status="pending", products=[product]),
            dict(printer_id=printer_id, enabled=True, lines=lines),
            [dict(product_id=product,label=label,active=active == 1,confirmed=True) for label,active in rows])
        if decision.accepted:
            selected = sku
            break
    if selected is None:
        raise ValueError("NO_CATALOG_PRODUCT_FOR_PRINTER_LINES")
    conn.execute("INSERT INTO wb_fbs_orders VALUES ('synthetic',%s,'confirm','pending')", (order_id,))
    conn.execute("INSERT INTO wb_fbs_picking_queue VALUES ('synthetic',%s)", (order_id,))
    conn.execute("INSERT INTO wb_fbs_order_skus VALUES ('synthetic',%s,%s)", (order_id,selected))
    conn.execute("INSERT INTO synthetic_pr192_print_stickers VALUES ('synthetic',%s,%s)",
                 (order_id,"SYNTHETIC-ONLY-" + request_id))
    reason, _ = _validate_before_dispatch(conn,account_id="synthetic",order_id=order_id,
                                          kind="WB_FBS",printer_id=printer_id)
    if reason != "ELIGIBLE":
        raise ValueError("ROUTING_REVALIDATION_FAILED")
    job = uuid.uuid4().hex
    conn.execute("""INSERT INTO synthetic_pr192_print_jobs
        (job_id,account_id,order_id,sticker_kind,printer_id,state,delivery_transport)
        VALUES (%s,'synthetic',%s,'WB_FBS',%s,'QUEUED','USB')""", (job,order_id,printer_id))
    conn.execute("""INSERT INTO synthetic_pr192_usb_approvals(ticket_id,job_id,printer_id,phase)
        VALUES (%s,%s,%s,'APPROVED')""", (request_id,job,printer_id))
    conn.execute("""INSERT INTO synthetic_pr192_print_events
        (action_id,job_id,actor_id,event,from_state,to_state,reason)
        VALUES (%s,%s,%s,'USB_TEST_APPROVAL',NULL,'QUEUED','ONE_SYNTHETIC_USB_ONLY')""",
        (uuid.uuid4().hex,job,manager_id))
    return dict(state="READY",ticket_id=request_id,stand_id=stand,order_id=order_id)
