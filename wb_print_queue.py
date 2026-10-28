"""Conservative print queue for saved WB batches. No USB commands.

The manager explicitly reserves a finite route selection. Claimed jobs are
never requeued automatically. An agent implementation is a separate stage.
"""
import hmac
import os
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from flask import Blueprint, jsonify, request
from wms import db_connect, manager_access, sql

wb_print_queue_bp = Blueprint("wb_print_queue", __name__)
TEST_PRINTER = "ONIX-TEST-XP460B"
MAX_JOBS = 100


def _now():
    return datetime.now(timezone.utc).isoformat()


def _schema():
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS wb_route_print_jobs (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, wb_code TEXT NOT NULL,
            article TEXT NOT NULL, page_no INTEGER NOT NULL, line TEXT NOT NULL,
            printer TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
            claimed_at TEXT, completed_at TEXT,
            UNIQUE(batch_id,wb_code)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wb_route_queue ON wb_route_print_jobs(status,printer,created_at)")
        conn.commit()
        cur.close()


def _origin_ok():
    target=urlsplit(request.headers.get("Origin") or request.headers.get("Referer") or "")
    return target.scheme=="https" and target.netloc==request.host


@wb_print_queue_bp.post("/api/manager/wb-batches/<batch_id>/print-queue")
def enqueue(batch_id):
    if not manager_access() or not _origin_ok():
        return jsonify(error="Доступ запрещён"),403
    try:
        uuid.UUID(batch_id)
    except (TypeError, ValueError):
        return jsonify(error="Неверный ID партии"),400
    data=request.get_json(silent=True) or {}
    if data.get("confirmation")!="reserve-onix-test-only":
        return jsonify(error="Требуется явное подтверждение"),400
    lines=data.get("lines")
    if not isinstance(lines,list) or not lines or len(lines)>4 or any(not isinstance(x,str) or len(x)!=1 or x not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for x in lines) or len(set(lines))!=len(lines):
        return jsonify(error="Укажи уникальные линии A-Z (до 4)"),400
    if data.get("printer")!=TEST_PRINTER:
        return jsonify(error="Разрешён только тестовый принтер"),400
    _schema()
    from wb_pdf_batches import _schema as batch_schema, classify_route
    batch_schema()
    with db_connect() as conn:
        cur=conn.cursor()
        batch=cur.execute(sql("SELECT status FROM wb_pdf_batches WHERE id=?"),(batch_id,)).fetchone()
        if not batch or batch[0]!="imported_review_only":
            return jsonify(error="Партия не найдена или не допущена"),409
        stickers=cur.execute(sql("""SELECT wb_code,article,page_no,location_label,route_status
                                   FROM wb_pdf_batch_stickers WHERE batch_id=? ORDER BY page_no"""),(batch_id,)).fetchall()
        selected=[]
        for code,article,page,location,status in stickers:
            line=classify_route(location,status,lines)
            if line in lines:
                selected.append((code,article,page,line))
        if not selected or len(selected)>MAX_JOBS:
            return jsonify(error="В партии должно быть от 1 до 100 заказов выбранных линий"),422
        created=0
        for code,article,page,line in selected:
            cur.execute(sql("""INSERT INTO wb_route_print_jobs
                (id,batch_id,wb_code,article,page_no,line,printer,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,wb_code) DO NOTHING"""),
                (str(uuid.uuid4()),batch_id,code,article,page,line,TEST_PRINTER,"queued",_now()))
            created+=cur.rowcount
        conn.commit()
        cur.close()
    return jsonify(ok=True,eligible=len(selected),created=created,
                   already_reserved=len(selected)-created,printer=TEST_PRINTER,
                   physical_prints=0),200


@wb_print_queue_bp.get("/api/manager/wb-print-queue")
def list_queue():
    if not manager_access():
        return jsonify(error="Доступ запрещён"),403
    _schema()
    with db_connect() as conn:
        cur=conn.cursor()
        rows=cur.execute(sql("""SELECT id,batch_id,wb_code,article,line,printer,status,created_at
                                FROM wb_route_print_jobs ORDER BY created_at DESC LIMIT 100""")).fetchall()
        cur.close()
    keys=("id","batch_id","wb_code","article","line","printer","status","created_at")
    return jsonify(jobs=[dict(zip(keys,r)) for r in rows])

# Read-only preview endpoint for a local Windows agent. It cannot claim a job
# or change queue state, and intentionally never returns PDF contents.
@wb_print_queue_bp.get("/api/print-agent/wb/preview-next")
def agent_preview_next():
    token=os.environ.get("ONIX_PRINT_AGENT_TOKEN","")
    supplied=request.headers.get("Authorization","")
    if len(token)<32 or not hmac.compare_digest(supplied,"Bearer "+token):
        return jsonify(error="Unauthorized"),401
    _schema()
    from wb_pdf_batches import _schema as batch_schema
    batch_schema()
    with db_connect() as conn:
        cur=conn.cursor()
        row=cur.execute(sql("""SELECT j.id,j.batch_id,j.wb_code,j.article,j.page_no,
                                      j.line,j.printer,b.sha256
                               FROM wb_route_print_jobs j
                               JOIN wb_pdf_batches b ON b.id=j.batch_id
                               WHERE j.status='queued' AND j.printer=?
                               ORDER BY j.created_at,j.id LIMIT 1"""),(TEST_PRINTER,)).fetchone()
        cur.close()
    if not row:
        return "",204
    keys=("job_id","batch_id","wb_code","article","page_no","line","printer","pdf_sha256")
    return jsonify(dict(zip(keys,row), mode="dry_run_only"))
