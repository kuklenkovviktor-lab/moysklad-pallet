"""Isolated GRIT demo -> single approved USB device claim (NO production routes).

Device token authenticates only this disposable demo. A manager must first
approve ONE queued synthetic job. Claim is destructive/idempotent: a crash
leaves SENDING, never QUEUED. Report never claims physical PRINTED.
"""
import os
import re
import secrets
import uuid

from flask import Blueprint, jsonify, request, session
from line_printer_pg_trial import _verify_isolated_connection
from services.printing.virtual_agent import _validate_before_dispatch
from services.printing.usb_test_preparation import prepare_locked, stand_identity

WINDOWS_QUEUE = "ONIX-TEST-XP460B"
DDL = """CREATE TABLE IF NOT EXISTS synthetic_pr192_usb_approvals (
  ticket_id text PRIMARY KEY,
  job_id text NOT NULL UNIQUE REFERENCES synthetic_pr192_print_jobs(job_id),
  printer_id text NOT NULL,
  phase text NOT NULL CHECK(phase IN ('APPROVED','CLAIMED','REPORTED','REJECTED')),
  result text NOT NULL DEFAULT '',
  spool_job_id bigint,
  approved_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
)"""


def initialize_synthetic_usb_bridge(conn):
    _verify_isolated_connection(conn)
    with conn.transaction():
        conn.execute(DDL)


def _audit(conn, job_id, old, new, reason):
    conn.execute("""INSERT INTO synthetic_pr192_print_events
      (action_id,job_id,actor_id,event,from_state,to_state,reason)
      VALUES (%s,%s,'synthetic-windows-usb-bridge','USB_AGENT',%s,%s,%s)""",
      (uuid.uuid4().hex, job_id, old, new, reason))


def _refuse(code, status=400):
    return jsonify(error=code, synthetic=True, physical_printing=False), status, {
        "Cache-Control": "no-store",
    }


def make_synthetic_usb_bridge_blueprint(connection_factory, manager_authorized):
    if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("USB bridge only available on isolated synthetic PostgreSQL")
    if not callable(manager_authorized):
        raise RuntimeError("Trusted manager guard is mandatory")
    token = os.environ.get("GRIT_PR192_USB_AGENT_TOKEN", "")
    if len(token) < 32 or len(token) > 256:
        raise RuntimeError("Fresh per-demo USB agent token required")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        initialize_synthetic_usb_bridge(conn)

    bp = Blueprint("pr192_synthetic_usb_bridge", __name__)

    def device_ok():
        received = request.headers.get("X-GRIT-USB-Agent-Token", "")
        return isinstance(received, str) and len(received) == len(token) and secrets.compare_digest(received, token)

    def manager_ok():
        return manager_authorized() is True

    def csrf_ok(body):
        actual = session.get("pr192_manager_csrf")
        value = body.get("csrf") if isinstance(body, dict) else None
        return (isinstance(actual, str) and len(actual) >= 16 and
                isinstance(value, str) and secrets.compare_digest(actual, value))

    @bp.post("/demo/printing/usb/prepare")
    def prepare_test():
        if not manager_ok():
            return _refuse("MANAGER_REQUIRED", 403)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"printer_id", "request_id", "csrf"}:
            return _refuse("INVALID_PREPARATION")
        if not csrf_ok(data):
            return _refuse("MANAGER_CSRF_REQUIRED", 403)
        if not all(isinstance(data[k], str) for k in ("printer_id", "request_id")):
            return _refuse("INVALID_PREPARATION")
        try:
            with connection_factory() as conn:
                with conn.transaction():
                    result = prepare_locked(conn, data["printer_id"], data["request_id"],
                                            session["pr192_manager_id"])
        except ValueError as exc:
            return _refuse(str(exc), 409)
        return jsonify(synthetic=True,physical_printing=False,**result)

    @bp.get("/demo/printing/usb/state")
    def manager_state():
        if not manager_ok():
            return _refuse("MANAGER_REQUIRED", 403)
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            rows = conn.execute("""SELECT a.ticket_id,j.order_id,a.phase,a.result,
                         a.approved_at,a.spool_job_id
                FROM synthetic_pr192_usb_approvals a
                JOIN synthetic_pr192_print_jobs j ON j.job_id=a.job_id
                ORDER BY a.approved_at DESC LIMIT 10""").fetchall()
        return jsonify(synthetic=True,physical_printing=False,enabled=True,
                       windows_printer=WINDOWS_QUEUE,
                       approvals=[{"ticket_id":r[0][:8],"order_id":r[1],
                                   "phase":r[2],"result":r[3],
                                   "approved_at":r[4].isoformat(),
                                   "spool_job_id":r[5]} for r in rows]),200, {
                           "Cache-Control":"no-store"}

    @bp.post("/demo/printing/usb/approve")
    def manager_approve():
        if not manager_ok():
            return _refuse("MANAGER_REQUIRED", 403)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"printer_id", "csrf"}:
            return _refuse("ONLY_PRINTER_AND_CSRF_ALLOWED")
        if not csrf_ok(data):
            return _refuse("MANAGER_CSRF_REQUIRED", 403)
        printer_id = data["printer_id"]
        if not isinstance(printer_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", printer_id):
            return _refuse("INVALID_PRINTER_ID")
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('GRIT-USB-ONE-APPROVAL',0))")
                active = conn.execute("""SELECT 1 FROM synthetic_pr192_usb_approvals
                    WHERE phase IN ('APPROVED','CLAIMED') LIMIT 1""").fetchone()
                if active:
                    return _refuse("ONE_USB_APPROVAL_ALREADY_ACTIVE", 409)
                enabled = conn.execute("""SELECT enabled FROM grit_line_printers
                    WHERE printer_id=%s FOR SHARE""",(printer_id,)).fetchone()
                if not enabled or enabled[0] != 1:
                    return _refuse("PRINTER_MISSING_OR_DISABLED",404)
                job = conn.execute("""SELECT job_id,account_id,order_id,sticker_kind
                    FROM synthetic_pr192_print_jobs
                    WHERE account_id='synthetic' AND printer_id=%s AND state='QUEUED'
                    ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (printer_id,)).fetchone()
                if not job:
                    return jsonify(synthetic=True, physical_printing=False,
                                   state="EMPTY", reason="NO_QUEUED_JOB"),200
                job_id,account_id,order_id,kind = job
                eligible, _ = _validate_before_dispatch(
                    conn,account_id=account_id,order_id=order_id,
                    kind=kind,printer_id=printer_id)
                if eligible != "ELIGIBLE":
                    conn.execute("""UPDATE synthetic_pr192_print_jobs
                        SET state='CANCELLED',version=version+1,updated_at=now()
                        WHERE job_id=%s""",(job_id,))
                    _audit(conn,job_id,"QUEUED","CANCELLED","USB_PRE_APPROVAL_STALE:"+eligible)
                    return jsonify(synthetic=True,physical_printing=False,
                                   state="CANCELLED",reason=eligible),200
                ticket=uuid.uuid4().hex
                conn.execute("UPDATE synthetic_pr192_print_jobs SET delivery_transport='USB' WHERE job_id=%s",(job_id,))
                conn.execute("""INSERT INTO synthetic_pr192_usb_approvals
                    (ticket_id,job_id,printer_id,phase)
                    VALUES (%s,%s,%s,'APPROVED')""",(ticket,job_id,printer_id))
        return jsonify(synthetic=True,physical_printing=False,
                       state="APPROVED",ticket_id=ticket[:8],order_id=order_id,
                       message="Manager approved exactly one synthetic USB claim"),200,{
                           "Cache-Control":"no-store"}

    @bp.get("/synthetic/pr192/usb-agent/ping")
    def agent_ping():
        if not device_ok():
            return _refuse("AGENT_TOKEN_REQUIRED",403)
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            count=conn.execute("""SELECT count(*)::int FROM synthetic_pr192_usb_approvals
                WHERE phase='APPROVED'""").fetchone()[0]
            stand = stand_identity(conn)
            approved = conn.execute("SELECT ticket_id FROM synthetic_pr192_usb_approvals WHERE phase='APPROVED'").fetchall()
        return jsonify(synthetic=True,device=WINDOWS_QUEUE,
                       waiting=count,stand_id=stand,
                       ticket_id=approved[0][0] if len(approved)==1 else None,
                       physical_printing=False),200,{
                           "Cache-Control":"no-store"}

    @bp.post("/synthetic/pr192/usb-agent/claim")
    def agent_claim():
        if not device_ok():
            return _refuse("AGENT_TOKEN_REQUIRED",403)
        data=request.get_json(silent=True)
        bound = isinstance(data,dict) and set(data)=={"ticket_id","stand_id"}
        if bound and (not isinstance(data["ticket_id"],str) or
                      not re.fullmatch("[0-9a-f]{32}",data["ticket_id"]) or
                      not isinstance(data["stand_id"],str)):
            return _refuse("INVALID_BINDING")
        if data != {} and not bound:
            return _refuse("EMPTY_BODY_REQUIRED")
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            with conn.transaction():
                if bound and data["stand_id"] != stand_identity(conn):
                    return _refuse("WRONG_STAND",403)
                claim=conn.execute("""SELECT ticket_id,job_id,printer_id
                    FROM synthetic_pr192_usb_approvals WHERE phase='APPROVED'
                    AND (%s::text IS NULL OR ticket_id=%s)
                    ORDER BY approved_at,ticket_id
                    FOR UPDATE SKIP LOCKED LIMIT 1""",
                    (data.get("ticket_id"),data.get("ticket_id"))).fetchone()
                if not claim:
                    return jsonify(synthetic=True,physical_printing=False,
                                   state="EMPTY"),200
                ticket,job_id,printer_id=claim
                job=conn.execute("""SELECT state,account_id,order_id,sticker_kind
                    FROM synthetic_pr192_print_jobs
                    WHERE job_id=%s FOR UPDATE""",(job_id,)).fetchone()
                if not job or job[0]!="QUEUED":
                    conn.execute("""UPDATE synthetic_pr192_usb_approvals
                        SET phase='REJECTED',result='NO_LONGER_QUEUED',updated_at=now()
                        WHERE ticket_id=%s""",(ticket,))
                    return jsonify(synthetic=True,physical_printing=False,
                                   state="REJECTED",reason="NO_LONGER_QUEUED"),200
                state,account_id,order_id,kind=job
                eligible,_=_validate_before_dispatch(
                    conn,account_id=account_id,order_id=order_id,
                    kind=kind,printer_id=printer_id)
                if eligible!="ELIGIBLE":
                    conn.execute("""UPDATE synthetic_pr192_print_jobs
                        SET state='CANCELLED',version=version+1,updated_at=now()
                        WHERE job_id=%s""",(job_id,))
                    _audit(conn,job_id,"QUEUED","CANCELLED","USB_PRE_CLAIM_STALE:"+eligible)
                    conn.execute("""UPDATE synthetic_pr192_usb_approvals
                        SET phase='REJECTED',result='STALE',updated_at=now()
                        WHERE ticket_id=%s""",(ticket,))
                    return jsonify(synthetic=True,physical_printing=False,
                                   state="REJECTED",reason="STALE"),200
                conn.execute("""UPDATE synthetic_pr192_print_jobs
                    SET state='SENDING',version=version+1,updated_at=now()
                    WHERE job_id=%s""",(job_id,))
                _audit(conn,job_id,"QUEUED","SENDING","USB_SINGLE_CLAIM")
                conn.execute("""UPDATE synthetic_pr192_usb_approvals
                    SET phase='CLAIMED',updated_at=now() WHERE ticket_id=%s""",
                    (ticket,))
        # There is NO original sticker, order payload or WB data in the agent response.
        return jsonify(synthetic=True,physical_printing=False,
                       state="CLAIMED",ticket_id=ticket,
                       windows_printer=WINDOWS_QUEUE),200,{"Cache-Control":"no-store"}

    @bp.post("/synthetic/pr192/usb-agent/report")
    def agent_report():
        if not device_ok():
            return _refuse("AGENT_TOKEN_REQUIRED",403)
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)!={"ticket_id","result","spool_job_id"}:
            return _refuse("INVALID_REPORT")
        ticket=data["ticket_id"]
        result=data["result"]
        spool_job_id=data["spool_job_id"]
        if not isinstance(ticket,str) or not re.fullmatch("[0-9a-f]{32}",ticket):
            return _refuse("INVALID_TICKET")
        if result not in ("SUBMITTED_TO_WINDOWS","UNKNOWN"):
            return _refuse("INVALID_RESULT")
        if result=="SUBMITTED_TO_WINDOWS":
            if isinstance(spool_job_id,bool) or not isinstance(spool_job_id,int) or spool_job_id<=0:
                return _refuse("SPOOL_JOB_ID_REQUIRED")
        elif spool_job_id is not None:
            return _refuse("UNKNOWN_MUST_NOT_ASSERT_SPOOL_ID")
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            with conn.transaction():
                approval=conn.execute("""SELECT job_id,phase FROM synthetic_pr192_usb_approvals
                    WHERE ticket_id=%s FOR UPDATE""",(ticket,)).fetchone()
                if not approval:
                    return _refuse("TICKET_NOT_FOUND",404)
                job_id,phase=approval
                if phase!="CLAIMED":
                    return _refuse("NOT_CLAIMED_OR_ALREADY_REPORTED",409)
                job=conn.execute("""SELECT state FROM synthetic_pr192_print_jobs
                    WHERE job_id=%s FOR UPDATE""",(job_id,)).fetchone()
                if not job or job[0]!="SENDING":
                    return _refuse("JOB_NOT_SENDING",409)
                target="SUBMITTED" if result=="SUBMITTED_TO_WINDOWS" else "UNKNOWN"
                conn.execute("""UPDATE synthetic_pr192_print_jobs
                    SET state=%s,version=version+1,updated_at=now()
                    WHERE job_id=%s""",(target,job_id))
                _audit(conn,job_id,"SENDING",target,
                       "USB_SPOOLER_ACK_NOT_PHYSICAL" if target=="SUBMITTED"
                       else "USB_OUTCOME_UNKNOWN_NO_RETRY")
                conn.execute("""UPDATE synthetic_pr192_usb_approvals SET
                    phase='REPORTED',result=%s,spool_job_id=%s,updated_at=now()
                    WHERE ticket_id=%s""",(result,spool_job_id,ticket))
        return jsonify(synthetic=True,physical_printing=False,state=target),200,{
            "Cache-Control":"no-store"}

    return bp
