"""Isolated virtual printer agent for GRIT (#192).

SIMULATES ONLY. No Windows API, USB, HTTP, print(), WB API, spooler or
real sticker delivery. Can connect only to grit_pr192_synthetic_* PostgreSQL
with explicit GRIT_SYNTHETIC_PG_TEST=YES_ISOLATED gate.

A virtual acknowledgment means SUBMITTED (accepted by a software sink),
never PRINTED (physical output). Crashes after claiming or acknowledging
leave SENDING, which must be reconciled to UNKNOWN, NOT resent.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import uuid

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_routing import route_order_to_printer
from services.printing.pg_queue import transition_synthetic_print_pg


RECEIPT_DDL = """CREATE TABLE IF NOT EXISTS synthetic_pr192_virtual_receipts (
   job_id text PRIMARY KEY REFERENCES synthetic_pr192_print_jobs(job_id),
   printer_id text NOT NULL,
   sticker_digest text NOT NULL,
   accepted_at timestamptz NOT NULL DEFAULT now()
)"""


@dataclass(frozen=True)
class VirtualDelivery:
    state: str
    reason: str
    job_id: str = ""


def initialize_virtual_sink(conn):
    _verify_isolated_connection(conn)
    with conn.transaction():
        conn.execute(RECEIPT_DDL)


def _check_identifier(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 160


def _audit(conn, job_id, actor_id, old, new, reason):
    conn.execute("""INSERT INTO synthetic_pr192_print_events
      (action_id,job_id,actor_id,event,from_state,to_state,reason)
      VALUES (%s,%s,%s,'VIRTUAL_AGENT',%s,%s,%s)""",
      (uuid.uuid4().hex,job_id,actor_id,old,new,reason))


def _validate_before_dispatch(conn, *, account_id, order_id, kind, printer_id):
    """Recheck CURRENT isolated source under PG row locks before sending.

    The virtual agent must not rely on a status checked only at QR scan time.
    This function does NOT use a passed-in QR and does NOT accept client fields.
    """
    if account_id != "synthetic" or kind != "WB_FBS":
        return "INVALID_NAMESPACE", ""
    row = conn.execute("""SELECT lines_json,enabled FROM grit_line_printers
        WHERE printer_id=%s FOR SHARE""", (printer_id,)).fetchone()
    if not row or row[1] != 1:
        return "PRINTER_DISABLED", ""
    try:
        lines=json.loads(row[0])
        if not isinstance(lines,list) or not lines:
            return "PRINTER_CONFIG_INVALID", ""
    except (ValueError,TypeError):
        return "PRINTER_CONFIG_INVALID", ""
    order = conn.execute("""SELECT wb_status,local_status FROM wb_fbs_orders
        WHERE account_id=%s AND order_id=%s FOR UPDATE""",
        (account_id,order_id)).fetchone()
    if not order:
        return "ORDER_MISSING", ""
    in_queue=conn.execute("""SELECT 1 FROM wb_fbs_picking_queue
        WHERE account_id=%s AND order_id=%s FOR SHARE""",
        (account_id,order_id)).fetchone()
    if not in_queue:
        return "ORDER_NOT_IN_PICKING_QUEUE", ""
    sticker=conn.execute("""SELECT original_sticker
        FROM synthetic_pr192_print_stickers
        WHERE account_id=%s AND order_id=%s FOR SHARE""",
        (account_id,order_id)).fetchone()
    if not sticker or not isinstance(sticker[0],str) or not sticker[0].strip():
        return "ORIGINAL_STICKER_MISSING", ""
    sku_rows=conn.execute("""SELECT sku FROM wb_fbs_order_skus
        WHERE account_id=%s AND order_id=%s FOR SHARE""",
        (account_id,order_id)).fetchall()
    if not sku_rows:
        return "ORDER_SKUS_MISSING", ""
    products=[]
    for (sku,) in sku_rows:
        match=conn.execute("""SELECT product_id FROM fbs_catalog_barcodes
            WHERE barcode=%s AND is_pack=0 AND quantity=1 FOR SHARE""",
            (sku,)).fetchone()
        if not match or not match[0]:
            return "CATALOG_BARCODE_MISSING", ""
        products.append(match[0])
    mappings=conn.execute("""SELECT a.product_id,l.label,l.active
        FROM cell_sku_assignments a
        JOIN replenishment_locations l ON l.id=a.location_id
        WHERE a.product_id=ANY(%s::text[])
        FOR SHARE OF a,l""", (products,)).fetchall()
    locs=[{"product_id":p,"label":label,"active":active == 1,
           "confirmed":True} for p,label,active in mappings]
    decision=route_order_to_printer(
        {"order_id":order_id,"wb_status":order[0],
         "local_status":order[1],"products":products},
        {"printer_id":printer_id,"enabled":True,"lines":lines},
        locs)
    if not decision.accepted:
        return decision.reason, ""
    return "ELIGIBLE", sticker[0]


def claim_next_virtual_job(connection_factory, *, printer_id, agent_id):
    """Atomically claim ONE queued job for one virtual printer.

    Stale orders are CANCELLED before any sink operation, with an audit row.
    FOR UPDATE SKIP LOCKED prevents two simulated agents from claiming a job.
    """
    if not _check_identifier(printer_id) or not _check_identifier(agent_id):
        raise ValueError("Invalid virtual agent identity")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            row=conn.execute("""SELECT job_id,account_id,order_id,sticker_kind
              FROM synthetic_pr192_print_jobs
              WHERE printer_id=%s AND state='QUEUED'
              ORDER BY created_at,job_id
              FOR UPDATE SKIP LOCKED LIMIT 1""",
              (printer_id,)).fetchone()
            if not row:
                return VirtualDelivery("EMPTY","NO_QUEUED_JOB"), ""
            job_id,account_id,order_id,kind=row
            result,sticker=_validate_before_dispatch(
                conn,account_id=account_id,order_id=order_id,
                kind=kind,printer_id=printer_id)
            if result != "ELIGIBLE":
                conn.execute("""UPDATE synthetic_pr192_print_jobs
                    SET state='CANCELLED',version=version+1,updated_at=now()
                    WHERE job_id=%s""", (job_id,))
                _audit(conn,job_id,agent_id,"QUEUED","CANCELLED",
                       "PRE_SEND_STALE:"+result)
                return VirtualDelivery("CANCELLED","PRE_SEND_STALE:"+result,job_id),""
            conn.execute("""UPDATE synthetic_pr192_print_jobs
                 SET state='SENDING',version=version+1,updated_at=now()
                 WHERE job_id=%s""",(job_id,))
            _audit(conn,job_id,agent_id,"QUEUED","SENDING","VIRTUAL_CLAIM")
            return VirtualDelivery("SENDING","VIRTUAL_CLAIMED",job_id),sticker


def _accept_at_virtual_sink(connection_factory, *, job_id,printer_id,sticker):
    """Persists only the synthetic sticker hash, never the original payload."""
    if not _check_identifier(job_id) or not _check_identifier(printer_id):
        raise ValueError("Invalid virtual receipt identifier")
    digest=sha256(sticker.encode("utf-8")).hexdigest()
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            row=conn.execute("""SELECT state,printer_id FROM synthetic_pr192_print_jobs
                WHERE job_id=%s FOR UPDATE""",(job_id,)).fetchone()
            if not row or row != ("SENDING",printer_id):
                raise RuntimeError("Virtual delivery requires claimed job")
            inserted=conn.execute("""INSERT INTO synthetic_pr192_virtual_receipts
                (job_id,printer_id,sticker_digest)
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                RETURNING job_id""",(job_id,printer_id,digest)).fetchone()
            if not inserted:
                prev=conn.execute("""SELECT printer_id,sticker_digest
                    FROM synthetic_pr192_virtual_receipts
                    WHERE job_id=%s""",(job_id,)).fetchone()
                if prev != (printer_id,digest):
                    raise RuntimeError("Virtual receipt conflict")


def _change_status(connection_factory, *, job_id,agent_id,target,evidence=""):
    return transition_synthetic_print_pg(
        connection_factory,job_id=job_id,action_id=uuid.uuid4().hex,
        actor_id=agent_id,target=target,
        authorized_locked=lambda conn,row,new: (
            row[4] is not None and new in ("SUBMITTED","UNKNOWN","FAILED")),
        reason="VIRTUAL_SINK_RESULT",evidence=evidence)


def deliver_one_virtual(connection_factory, *, printer_id, agent_id,
                        mode="ACK"):
    """Claim one job, optionally simulate a virtual sink outcome.

    Modes:
      ACK: sink records receipt; job SUBMITTED (NOT PRINTED).
      CERTAIN_FAILURE: no sink call; job FAILED with known-not-delivered proof.
      UNKNOWN_AFTER_ACCEPT: receipt recorded, lost ACK; job UNKNOWN.
      CRASH_BEFORE_SINK: leave SENDING and no receipt.
      CRASH_AFTER_ACCEPT: receipt recorded; leave SENDING for reconciliation.

    Every mode is explicitly synthetic. Never automatically retry UNCERTAIN.
    """
    if mode not in ("ACK","CERTAIN_FAILURE","UNKNOWN_AFTER_ACCEPT",
                    "CRASH_BEFORE_SINK","CRASH_AFTER_ACCEPT"):
        raise ValueError("Invalid synthetic delivery mode")
    claim,sticker=claim_next_virtual_job(
        connection_factory,printer_id=printer_id,agent_id=agent_id)
    if claim.state != "SENDING":
        return claim
    if mode=="CRASH_BEFORE_SINK":
        return VirtualDelivery("SENDING","SIMULATED_CRASH_BEFORE_SINK",claim.job_id)
    if mode=="CERTAIN_FAILURE":
        result=_change_status(connection_factory,job_id=claim.job_id,
                              agent_id=agent_id,target="FAILED",
                              evidence="CERTAIN_NOT_PRINTED")
        if not result.accepted:
            raise RuntimeError("Virtual known-failure status update rejected")
        return VirtualDelivery("FAILED","CERTAIN_NOT_DELIVERED",claim.job_id)
    _accept_at_virtual_sink(connection_factory,job_id=claim.job_id,
                            printer_id=printer_id,sticker=sticker)
    if mode=="CRASH_AFTER_ACCEPT":
        return VirtualDelivery("SENDING","SIMULATED_CRASH_AFTER_ACCEPT",claim.job_id)
    target="UNKNOWN" if mode=="UNKNOWN_AFTER_ACCEPT" else "SUBMITTED"
    result=_change_status(connection_factory,job_id=claim.job_id,
                          agent_id=agent_id,target=target)
    if not result.accepted:
        raise RuntimeError("Virtual sink acknowledgment status update rejected")
    return VirtualDelivery(target,"VIRTUAL_SINK_"+target,claim.job_id)


def reconcile_stale_virtual_sending(connection_factory, *, agent_id,
                                    older_than_seconds=300):
    """Mark stale SENDING as UNKNOWN without resending anything.

    This is NOT a worker. Caller invokes it explicitly in isolated tests.
    No automatic attempt to infer physical printer status from receipt rows.
    """
    if not _check_identifier(agent_id) or not (
            isinstance(older_than_seconds,int) and older_than_seconds >= 1):
        raise ValueError("Invalid synthetic reconciliation arguments")
    total=0
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            rows=conn.execute("""SELECT job_id FROM synthetic_pr192_print_jobs
                WHERE state='SENDING'
                 AND updated_at < now()-(%s::integer * interval '1 second')
                ORDER BY updated_at,job_id FOR UPDATE SKIP LOCKED""",
                (older_than_seconds,)).fetchall()
            for (job_id,) in rows:
                conn.execute("""UPDATE synthetic_pr192_print_jobs
                    SET state='UNKNOWN',version=version+1,updated_at=now()
                    WHERE job_id=%s""",(job_id,))
                _audit(conn,job_id,agent_id,"SENDING","UNKNOWN",
                       "STALE_INFLIGHT_NO_PHYSICAL_PROOF")
                total += 1
    return total
