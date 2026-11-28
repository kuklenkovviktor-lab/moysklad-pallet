"""OFF-BY-DEFAULT isolated PR211 original-PDF printing HTTP blueprint.

Inject real authenticated manager/worker adapters in the *test harness*.
NOT registered in production app.py and refuses non-disposable PG17.
The agent receives pre-rasterized original PDF bytes only after server-side
printer-scoped device auth and a signed worker printer-QR release.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets

from flask import Blueprint, jsonify, request, session
from services.printing.wb_original_pg17_outbox import (
    _check, stage, claim, report, approve_worker_qr, queue_status,
    prune_printed_payloads, LabelSourceError,
)
from services.printing.wb_original_pg17_device_auth import (
    initialize as init_devices, enroll, authenticate, revoke, status,
    AuthenticationFailed,
)

MAX_BYTES = 25*1024*1024
_QR = re.compile(r'^GRIT-PRINTER:1:([A-Za-z0-9_-]{1,40}):([A-Za-z0-9_-]{40,80})$')
_ID = re.compile(r'^[A-Za-z0-9_-]{1,40}$')


def make_isolated_original_print_blueprint(connection_factory, *,
                                          key, manager_guard, worker_guard):
    """Create test blueprint only. Never infer an authenticated role from JSON.

    manager_guard() -> bool from server session and manager_access()
    worker_guard() -> {'employee_id','shift_id','picking_active'} or None,
    verified against actual badge, active attendance and WMS picking activity.
    """
    from services.printing.wb_original_pg17_outbox import _key
    _key(key)
    if not callable(manager_guard) or not callable(worker_guard):
        raise ValueError('TRUSTED_SESSION_GUARDS_REQUIRED')
    bp=Blueprint('pr211_original_synthetic','pr211_original_synthetic')

    @bp.after_request
    def no_cache_secrets(response):
        # JSON payload may contain the one-time device token or original
        # WB source bitmap. Never cache these HTTP responses anywhere.
        response.headers['Cache-Control']='no-store, private'
        response.headers['Pragma']='no-cache'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        return response

    def manager():
        return manager_guard() is True

    def identity():
        user=worker_guard()
        if (not isinstance(user,dict) or user.get('picking_active') is not True
                or not isinstance(user.get('employee_id'),str)
                or not isinstance(user.get('shift_id'),str)
                or not user['employee_id'] or not user['shift_id']):
            return None
        return user

    def csrf(kind):
        name='pr211_'+kind+'_csrf'
        if name not in session:
            session[name]=secrets.token_urlsafe(32)
        return session[name]

    def check_csrf(kind, supplied):
        value=session.get('pr211_'+kind+'_csrf')
        return (isinstance(value,str) and isinstance(supplied,str)
                and hmac.compare_digest(value,supplied))

    def printer(conn,pid):
        if not isinstance(pid,str) or not _ID.fullmatch(pid):
            raise ValueError('INVALID_PRINTER')
        row=conn.execute("""SELECT name,lines_json,token_digest,enabled
            FROM grit_line_printers WHERE printer_id=%s""",(pid,)).fetchone()
        if not row or row[3] != 1:
            raise ValueError('PRINTER_INACTIVE_OR_MISSING')
        try:
            lines=json.loads(row[1])
        except (ValueError,TypeError):
            raise ValueError('PRINTER_LINES_MALFORMED') from None
        if (not isinstance(lines,list) or not lines or len(lines)>20
                or any(not isinstance(line,str) or not re.fullmatch(
                    r'[А-ЯЁA-Z]{1,3}',line) for line in lines)):
            raise ValueError('PRINTER_LINES_UNVERIFIED')
        return row,tuple(lines)

    def agent_printer(conn):
        # No caller-supplied printer ID; server resolves it from the credential.
        device=request.headers.get('X-GRIT-Device-ID','')
        header=request.headers.get('Authorization','')
        if not header.startswith('Bearer ') or len(header)>256:
            raise AuthenticationFailed('DEVICE_AUTH_FAILED')
        pid=authenticate(conn,device_id=device,bearer_token=header[7:])
        printer(conn,pid)  # refuse revoked/disabled printer
        return pid,device

    @bp.get('/synthetic/pr211/manager/session')
    def manager_session():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        return jsonify(synthetic=True,csrf=csrf('manager'))

    @bp.get('/synthetic/pr211/worker/session')
    def worker_session():
        if identity() is None:
            return jsonify(error='ACTIVE_PICKING_SHIFT_REQUIRED'),403
        return jsonify(synthetic=True,csrf=csrf('worker'))

    @bp.post('/synthetic/pr211/manager/pdf/stage')
    def stage_file():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        if not check_csrf('manager',request.form.get('csrf')):
            return jsonify(error='CSRF_REQUIRED'),403
        if request.content_length is not None and request.content_length>MAX_BYTES+65536:
            return jsonify(error='PDF_TOO_LARGE'),413
        upload=request.files.get('file')
        if not upload or not (upload.filename or '').lower().endswith('.pdf'):
            return jsonify(error='ORIGINAL_PDF_REQUIRED'),400
        raw=upload.stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:
            return jsonify(error='PDF_TOO_LARGE'),413
        try:
            with connection_factory() as conn:
                _check(conn)
                pid=request.form.get('printer_id','')
                _,lines=printer(conn,pid)
                # Do not transmit or retain original file bytes, only encrypted
                # bitmap payloads of unambiguously validated original pages.
                count=stage(conn,raw,account_id='synthetic',printer_id=pid,
                            verified_lines=lines,key=key,require_qr=True)
                result=queue_status(conn,printer_id=pid)
            return jsonify(synthetic=True,staged=count,states=result,
                           awaiting_worker_qr=True)
        except (ValueError,LabelSourceError) as exc:
            return jsonify(error=str(exc)),422

    @bp.post('/synthetic/pr211/worker/printer-qr')
    def scan_printer():
        user=identity()
        if user is None:
            return jsonify(error='ACTIVE_PICKING_SHIFT_REQUIRED'),403
        data=request.get_json(silent=True) or {}
        if not isinstance(data,dict) or not check_csrf('worker',data.get('csrf')):
            return jsonify(error='CSRF_REQUIRED'),403
        qr=data.get('qr')
        matched=_QR.fullmatch(qr) if isinstance(qr,str) and len(qr)<=160 else None
        if not matched:
            return jsonify(error='PRINTER_QR_INVALID'),403
        pid=matched.group(1)
        try:
            with connection_factory() as conn:
                _check(conn)
                row,lines=printer(conn,pid)
                digest=hashlib.sha256(qr.encode()).hexdigest()
                if not hmac.compare_digest(digest,str(row[2])):
                    return jsonify(error='PRINTER_QR_REVOKED'),403
                released=approve_worker_qr(conn,printer_id=pid,
                    worker_id=user['employee_id'],shift_id=user['shift_id'],
                    signed_qr_valid=True)
                result=queue_status(conn,printer_id=pid)
            return jsonify(synthetic=True,printer=row[0],lines=lines,
                           released=released,states=result)
        except (ValueError,LabelSourceError) as exc:
            return jsonify(error=str(exc)),409

    @bp.post('/synthetic/pr211/manager/devices/enroll')
    def enroll_device():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        data=request.get_json(silent=True) or {}
        if not check_csrf('manager',data.get('csrf')):
            return jsonify(error='CSRF_REQUIRED'),403
        try:
            with connection_factory() as conn:
                _check(conn)
                pid=data.get('printer_id')
                printer(conn,pid)
                init_devices(conn)
                created=enroll(conn,printer_id=pid,manager_authorized=True)
            return jsonify(synthetic=True,**created),201
        except ValueError as exc:
            return jsonify(error=str(exc)),422

    @bp.post('/synthetic/pr211/manager/devices/revoke')
    def revoke_device():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        data=request.get_json(silent=True) or {}
        if not check_csrf('manager',data.get('csrf')):
            return jsonify(error='CSRF_REQUIRED'),403
        with connection_factory() as conn:
            _check(conn)
            changed=revoke(conn,device_id=data.get('device_id'),
                           manager_authorized=True)
        return jsonify(synthetic=True,revoked=changed)

    @bp.get('/synthetic/pr211/manager/overview')
    def overview():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        with connection_factory() as conn:
            _check(conn)
            init_devices(conn)
            devices=status(conn)
            rows=conn.execute("""SELECT printer_id,state,COUNT(*)
              FROM synthetic_pr211_original_jobs
              GROUP BY printer_id,state ORDER BY printer_id,state""").fetchall()
            events=conn.execute("""SELECT j.printer_id,e.event,e.created_at
               FROM synthetic_pr211_original_events e
               JOIN synthetic_pr211_original_jobs j ON j.job_id=e.job_id
               ORDER BY e.created_at DESC LIMIT 30""").fetchall()
        return jsonify(synthetic=True,devices=devices,
            queues=[dict(printer_id=x[0],state=x[1],count=x[2]) for x in rows],
            events=[dict(printer_id=x[0],event=x[1],
                         when=x[2].isoformat()) for x in events])

    @bp.post('/synthetic/pr211/manager/retention')
    def retention():
        if not manager():
            return jsonify(error='MANAGER_REQUIRED'),403
        data=request.get_json(silent=True) or {}
        if not check_csrf('manager',data.get('csrf')):
            return jsonify(error='CSRF_REQUIRED'),403
        try:
            with connection_factory() as conn:
                count=prune_printed_payloads(conn,
                    retention_hours=data.get('hours',24),manager_authorized=True)
            return jsonify(synthetic=True,erased=count)
        except ValueError as exc:
            return jsonify(error=str(exc)),422

    @bp.post('/synthetic/pr211/agent/claim')
    def agent_claim():
        try:
            with connection_factory() as conn:
                _check(conn)
                pid,device_id=agent_printer(conn)
                result=claim(conn,printer_id=pid,key=key,
                             device_authenticated=True,device_id=device_id)
            if result is None:
                return jsonify(synthetic=True,state='EMPTY',
                               physical_printing=False)
            ticket,payload=result
            if len(payload)>65536:
                raise LabelSourceError('SOURCE_BITMAP_TOO_LARGE')
            return jsonify(synthetic=True,physical_printing=False,
                 state='CLAIMED',ticket_id=ticket,
                 payload_b64=base64.b64encode(payload).decode('ascii'),
                 payload_sha256=hashlib.sha256(payload).hexdigest(),
                 kind='ORIGINAL_PDF_BITMAP_SYNTHETIC_ONLY')
        except (AuthenticationFailed,ValueError):
            return jsonify(error='DEVICE_AUTH_FAILED'),401

    @bp.post('/synthetic/pr211/agent/report')
    def agent_report():
        try:
            with connection_factory() as conn:
                _check(conn)
                pid,device_id=agent_printer(conn)
                data=request.get_json(silent=True) or {}
                if not isinstance(data,dict):
                    return jsonify(error='INVALID_ACK'),400
                state=report(conn,printer_id=pid,
                             ticket=data.get('ticket_id'),result=data.get('result'),
                             device_authenticated=True,device_id=device_id)
            return jsonify(synthetic=True,state=state,
                           physical_printing=False)
        except AuthenticationFailed:
            return jsonify(error='DEVICE_AUTH_FAILED'),401
        except (ValueError,LabelSourceError):
            return jsonify(error='INVALID_ACK_OR_CLAIM'),409

    return bp

def verified_wms_manager_guard():
    """Adapter to the existing GRIT manager session, not a demo password."""
    from wms import manager_access
    return manager_access() is True


def verified_wms_picking_guard():
    """Use actual GRIT badge/attendance/open shift/picking activity checks.

    Returns None on any missing or ambiguous current session. This adapter
    is not automatically registered in app.py and does not use real data
    unless explicitly wired by an authorized deployment.
    """
    from wms import get_session_employee, query_one, sql
    emp=get_session_employee()
    if (not emp or session.get('manager_web')
            or session.get('attendance_verified') is not True):
        return None
    shift=str(session.get('employee_shift_id') or '')
    if not shift:
        return None
    active=query_one(sql("""SELECT id FROM shifts WHERE id=? AND employee_id=?
         AND ended_at IS NULL"""),(shift,emp['id']))
    if not active:
        return None
    picking=query_one(sql("""SELECT 1 FROM activities
         WHERE shift_id=? AND employee_id=? AND activity_type='picking'
           AND ended_at IS NULL LIMIT 1"""),(shift,emp['id']))
    if not picking:
        return None
    return dict(employee_id=emp['id'],shift_id=shift,picking_active=True)
