"""ONIX WMS: replenishment trip timer. Does not modify stock balances."""
import io
import threading
import uuid
from datetime import datetime, timezone, timedelta

import qrcode
from flask import Blueprint, jsonify, redirect, request, Response, render_template
from wms import DATABASE_URL, db_connect, ensure_db, get_session_employee, manager_access, now_iso, parse_iso, query_all, query_one, sql, log_event

timer_bp = Blueprint("timer", __name__)
_guard = threading.Lock()
_initialized = False
RESERVES = {"ONIX-RESERVE-3":"3", "ONIX-RESERVE-4":"4"}

def db_ready():
    global _initialized
    ensure_db()
    if _initialized: return
    with _guard:
        if _initialized: return
        with db_connect() as conn:
            cur=conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS replenishment_trip_times (
                id TEXT PRIMARY KEY, shift_id TEXT NOT NULL, employee_id TEXT NOT NULL,
                location_code TEXT NOT NULL, location_label TEXT NOT NULL,
                location_verified INTEGER NOT NULL DEFAULT 0, reserve TEXT,
                started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL DEFAULT 'active'
            )""")
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_trip_open ON replenishment_trip_times(employee_id) WHERE status='active'")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_trip_date ON replenishment_trip_times(started_at, employee_id)")
            conn.commit()
            cur.close()
        _initialized=True

SEL="SELECT id, location_code, location_label, location_verified, reserve, started_at, ended_at, status FROM replenishment_trip_times "

def info(row):
    if not row: return None
    start=parse_iso(row[5]); end=parse_iso(row[6]) if row[6] else datetime.now(timezone.utc)
    secs=max(0,int((end-start).total_seconds())) if start and end else 0
    return dict(id=row[0], code=row[1], location=row[2], verified=bool(row[3]),
                reserve=row[4], started_at=row[5], ended_at=row[6], status=row[7], seconds=secs)

def context():
    db_ready()
    e=get_session_employee()
    if not e:return None,None,(jsonify(error="Войди по бейджу"),401)
    r=query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",(e["id"],))
    if not r:return e,None,(jsonify(error="Сначала начни смену в FBS"),409)
    return e,r[0],None

def lock_shift(cur,sid,employee_id):
    if not DATABASE_URL:cur.execute("BEGIN IMMEDIATE")
    q="SELECT id FROM shifts WHERE id=? AND employee_id=? AND ended_at IS NULL"
    if DATABASE_URL:q+=" FOR UPDATE"
    cur.execute(sql(q),(sid,employee_id))
    return bool(cur.fetchone())

def change_activity(cur,sid,employee_id,kind,now):
    cur.execute(sql("SELECT activity_type FROM activities WHERE shift_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"),(sid,))
    r=cur.fetchone()
    if r and r[0]==kind:return
    cur.execute(sql("UPDATE activities SET ended_at=? WHERE shift_id=? AND ended_at IS NULL"),(now,sid))
    cur.execute(sql("INSERT INTO activities (id,shift_id,employee_id,activity_type,started_at,ended_at) VALUES (?,?,?,?,?,NULL)"),
                (str(uuid.uuid4()),sid,employee_id,kind,now))

@timer_bp.get("/replenishment-time")
def worker():
    db_ready()
    if not get_session_employee():return redirect("/login")
    return render_template("replenishment_timer.html")

@timer_bp.get("/api/replenishment-time/state")
def state():
    e,sid,error=context()
    if error:return error
    active=query_one(SEL+"WHERE employee_id=? AND status='active'",(e["id"],))
    rows=query_all("SELECT started_at, ended_at FROM replenishment_trip_times WHERE shift_id=? AND status='completed'",(sid,))
    seconds=sum(max(0,int((parse_iso(b)-parse_iso(a)).total_seconds())) for a,b in rows if parse_iso(a) and parse_iso(b))
    return jsonify(ok=True,employee=e["name"],active=info(active),trips=len(rows),seconds=seconds)

@timer_bp.post("/api/replenishment-time/start")
def start():
    e,sid,error=context()
    if error:return error
    data=request.get_json(silent=True) or {}
    code=str(data.get("location") or "").strip().upper()
    if not 1<=len(code)<=80 or code in RESERVES:
        return jsonify(error="Нужен ШК ячейки отбора"),400
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if not lock_shift(cur,sid,e["id"]):return jsonify(error="Смена закрыта"),409
            cur.execute(sql(SEL+"WHERE employee_id=? AND status='active'"),(e["id"],))
            old=cur.fetchone()
            if old:return jsonify(ok=True,active=info(old),resumed=True)
            cur.execute(sql("SELECT label FROM replenishment_locations WHERE active=1 AND (UPPER(barcode)=? OR UPPER(label)=?) LIMIT 1"),(code,code))
            r=cur.fetchone()
            label=r[0] if r else code
            verified=1 if r else 0
            now=now_iso();rid=str(uuid.uuid4())
            cur.execute(sql("INSERT INTO replenishment_trip_times (id,shift_id,employee_id,location_code,location_label,location_verified,started_at) VALUES (?,?,?,?,?,?,?)"),
                        (rid,sid,e["id"],code,label,verified,now))
            change_activity(cur,sid,e["id"],"replenishment",now)
            conn.commit()
        finally:cur.close()
    log_event(e["id"],"replenishment","trip_start",shift_id=sid,object_code=code,result=label)
    return jsonify(ok=True,active=info((rid,code,label,verified,None,now,None,"active")))

def close(status,reserve):
    e,sid,error=context()
    if error:return error
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if not lock_shift(cur,sid,e["id"]):return jsonify(error="Смена закрыта"),409
            cur.execute(sql(SEL+"WHERE employee_id=? AND shift_id=? AND status='active'"),(e["id"],sid))
            old=cur.fetchone()
            if not old:return jsonify(error="Нет активного пополнения. Обнови экран."),409
            now=now_iso()
            cur.execute(sql("UPDATE replenishment_trip_times SET ended_at=?,status=?,reserve=? WHERE id=?"),(now,status,reserve,old[0]))
            change_activity(cur,sid,e["id"],"picking",now)
            conn.commit()
        finally:cur.close()
    result=info((old[0],old[1],old[2],old[3],reserve,old[5],now,status))
    log_event(e["id"],"replenishment","trip_finish" if reserve else "trip_cancel",
              shift_id=sid,object_code=old[1],quantity=result["seconds"],result=reserve or "cancelled")
    return jsonify(ok=True,trip=result)

@timer_bp.post("/api/replenishment-time/finish")
def finish():
    code=str((request.get_json(silent=True) or {}).get("barcode") or "").strip().upper()
    if code not in RESERVES:return jsonify(error="Нужен QR склада №3 или №4"),400
    return close("completed",RESERVES[code])

@timer_bp.post("/api/replenishment-time/cancel")
def cancel():
    return close("cancelled",None)

@timer_bp.get("/manager/replenishment-time")
def manager():
    db_ready()
    if not manager_access():return redirect("/manager-login")
    return render_template("replenishment_timer_manager.html")

@timer_bp.get("/api/manager/replenishment-time")
def report():
    db_ready()
    if not manager_access():return jsonify(error="Доступ руководителю"),403
    day=request.args.get("date","")
    try:
        parsed=datetime.strptime(day,"%Y-%m-%d") if day else datetime.now(timezone(timedelta(hours=3))).replace(tzinfo=None)
    except ValueError:return jsonify(error="Неверная дата"),400
    local=parsed.replace(tzinfo=timezone(timedelta(hours=3)))
    first=local.astimezone(timezone.utc).isoformat()
    last=(local+timedelta(days=1)).astimezone(timezone.utc).isoformat()
    rows=query_all("SELECT e.name,r.location_label,r.reserve,r.started_at,r.ended_at,r.status,r.location_verified FROM replenishment_trip_times r JOIN employees e ON e.id=r.employee_id WHERE r.started_at>=? AND r.started_at<? ORDER BY r.started_at DESC",(first,last))
    items=[]
    for name,loc,res,started,ended,status,verified in rows:
        sec=max(0,int((parse_iso(ended)-parse_iso(started)).total_seconds())) if ended else None
        items.append(dict(employee=name,location=loc,reserve=res,started_at=started,ended_at=ended,status=status,seconds=sec,verified=bool(verified)))
    return jsonify(ok=True,rows=items,date=local.strftime("%Y-%m-%d"))

@timer_bp.get("/manager/replenishment-time/qr.png")
def store_qr():
    db_ready()
    if not manager_access():return jsonify(error="Доступ руководителю"),403
    store=request.args.get("store","")
    if store not in ("3","4"):return jsonify(error="Неверный склад"),400
    out=io.BytesIO()
    qrcode.make("ONIX-RESERVE-"+store).save(out,format="PNG")
    return Response(out.getvalue(),content_type="image/png")
