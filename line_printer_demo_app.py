"""GRIT printer-routing *isolated* browser demo, NOT production WMS.

Disposable PostgreSQL 17 + disposable SQLite employees. Bound only to localhost
by deploy/run_line_printer_demo.sh. Never uses a real WB token or print agent.
The existing production app.py does NOT import this module.
"""
import json
import os
import secrets
import uuid
from html import escape
from pathlib import Path

import psycopg
from flask import Flask, Response, jsonify, redirect, request, session

from line_printer_pg_lifecycle import initialize_lifecycle
from line_printer_pg_trial import _verify_isolated_connection
from line_printer_registry import create_printer, printable_58x40_html
from line_printer_session_gate import require_bound_terminal_identity
from line_printer_wms_auth_adapter import make_real_wms_auth_synthetic_blueprint
from line_printer_pg_integrated_trial import validate_locked_synthetic
from wms import get_session_employee, init_wms_db, manager_access, db_connect
from services.printing.pg_queue import initialize_synthetic_print_pg
from services.printing.qr_scan import initialize_synthetic_stickers
from services.printing.qr_http_synthetic import make_real_wms_synthetic_autoqueue_blueprint
from services.printing.worker_ui_synthetic import make_synthetic_worker_scanner_blueprint
from services.printing.virtual_agent import initialize_virtual_sink
from services.printing.manager_ui_synthetic import make_synthetic_printing_manager_blueprint
from services.printing.usb_bridge_synthetic import make_synthetic_usb_bridge_blueprint

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise RuntimeError("Refusing to start standalone demo without isolated gate")
DSN = os.environ.get("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise RuntimeError("Refusing non-demo database URL")
if not os.environ.get("GRIT_PR192_DEMO_PASSWORD"):
    raise RuntimeError("Standalone demo manager password missing")
if not os.environ.get("GRIT_PR192_DEMO_SECRET"):
    raise RuntimeError("Standalone demo session key missing")


def pg():
    connection = psycopg.connect(DSN)
    try:
        _verify_isolated_connection(connection)
    except BaseException:
        connection.close()
        raise
    return connection


SCHEMA = (
    "CREATE TABLE IF NOT EXISTS employees(id text primary key,active integer)",
    "CREATE TABLE IF NOT EXISTS shifts(id text primary key,employee_id text,ended_at text)",
    "CREATE TABLE IF NOT EXISTS activities(id text primary key,shift_id text,employee_id text,activity_type text,ended_at text)",
    "CREATE TABLE IF NOT EXISTS grit_line_printers(printer_id text primary key,name text,lines_json text,token_digest text,enabled integer)",
    "CREATE TABLE IF NOT EXISTS wb_fbs_orders(account_id text,order_id text,wb_status text,local_status text,primary key(account_id,order_id))",
    "CREATE TABLE IF NOT EXISTS wb_fbs_picking_queue(account_id text,order_id text,primary key(account_id,order_id))",
    "CREATE TABLE IF NOT EXISTS wb_fbs_order_skus(account_id text,order_id text,sku text,primary key(account_id,order_id,sku))",
    "CREATE TABLE IF NOT EXISTS fbs_catalog_barcodes(barcode text primary key,product_id text,quantity double precision,is_pack integer)",
    "CREATE TABLE IF NOT EXISTS replenishment_locations(id text primary key,label text,active integer)",
    "CREATE TABLE IF NOT EXISTS cell_sku_assignments(location_id text,product_id text,primary key(location_id,product_id))",
    "CREATE TABLE IF NOT EXISTS scans(id text primary key,shift_id text,employee_id text,wb_order_id text,marketplace text)",
)


def seed():
    # No production imports and NO external services. Entire data set invented.
    init_wms_db()
    with db_connect() as conn:
        conn.execute("""INSERT INTO employees(id,name,role,active,badge_code)
          VALUES('demo-e1','Демо-сборщик','staff',1,'DEMO-BADGE-1')
          ON CONFLICT(id) DO UPDATE SET active=1""")
        conn.execute("""INSERT INTO shifts(id,employee_id,started_at,ended_at)
          VALUES('demo-shift-1','demo-e1','2026-11-25T10:00:00',NULL)
          ON CONFLICT(id) DO UPDATE SET ended_at=NULL""")
        conn.commit()
    with pg() as conn:
        initialize_lifecycle(conn)
        for statement in SCHEMA:
            conn.execute(statement)
        # Separate synthetic queue and invented WB sticker fixture, never prod.
        initialize_synthetic_print_pg(conn)
        initialize_synthetic_stickers(conn)
        initialize_virtual_sink(conn)
        conn.execute("INSERT INTO employees VALUES('demo-e1',1) ON CONFLICT DO NOTHING")
        conn.execute("INSERT INTO shifts VALUES('demo-shift-1','demo-e1',NULL) ON CONFLICT DO NOTHING")
        if not conn.execute("SELECT 1 FROM activities WHERE shift_id='demo-shift-1'").fetchone():
            conn.execute("INSERT INTO activities VALUES('act1','demo-shift-1','demo-e1','picking',NULL)")
        for loc, label, product, sku in (
            ("loc-a","А-05-1","synthetic-product-a","SYNTH-SKU-A"),
            ("loc-b","Б-04-1","synthetic-product-b","SYNTH-SKU-B"),
            ("loc-v","В-03-1","synthetic-product-v","SYNTH-SKU-V"),
        ):
            conn.execute("INSERT INTO replenishment_locations VALUES(%s,%s,1) ON CONFLICT DO NOTHING", (loc,label))
            conn.execute("INSERT INTO cell_sku_assignments VALUES(%s,%s) ON CONFLICT DO NOTHING", (loc,product))
            conn.execute("INSERT INTO fbs_catalog_barcodes VALUES(%s,%s,1,0) ON CONFLICT DO NOTHING", (sku,product))
        for order, sku in (("SYNTH-1001","SYNTH-SKU-A"),("SYNTH-1002","SYNTH-SKU-B"),
                            ("SYNTH-1003","SYNTH-SKU-V")):
            conn.execute("""INSERT INTO wb_fbs_orders VALUES
                ('synthetic',%s,'confirm','pending') ON CONFLICT DO NOTHING""", (order,))
            conn.execute("""INSERT INTO wb_fbs_picking_queue VALUES
                ('synthetic',%s) ON CONFLICT DO NOTHING""", (order,))
            conn.execute("""INSERT INTO wb_fbs_order_skus VALUES
                ('synthetic',%s,%s) ON CONFLICT DO NOTHING""", (order,sku))
            conn.execute("""INSERT INTO synthetic_pr192_print_stickers VALUES
                ('synthetic',%s,%s) ON CONFLICT DO NOTHING""",
                (order, "SYNTHETIC-ONLY-STICKER-"+order))


app = Flask("grit_pr192_demo")
app.secret_key = os.environ["GRIT_PR192_DEMO_SECRET"]
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Strict")
app.register_blueprint(make_real_wms_auth_synthetic_blueprint(pg))
seed()
app.register_blueprint(make_real_wms_synthetic_autoqueue_blueprint(pg))
# Expose ONLY the existing /fbs screen and the few existing WMS read/session
# handlers needed for synthetic printer-QR browser verification. Unlike
# registering the entire WMS blueprint, this does not expose unrelated live
# WMS import, marketplace, worker scanning, or admin endpoints.
# This application itself is guarded by isolated-PG env + loopback Docker port.
import wms as existing_wms

@app.get("/fbs")
def demo_existing_fbs_screen():
    response = existing_wms.fbs_page()
    return response

@app.get("/api/auth/me")
def demo_existing_auth_me():
    return existing_wms.auth_me()

@app.get("/api/shift/current")
def demo_existing_shift_current():
    return existing_wms.current_shift_api()

@app.get("/api/fbs/stats")
def demo_existing_fbs_stats():
    return existing_wms.stats()

@app.get("/api/fbs/pending")
def demo_existing_pending():
    return existing_wms.fbs_pending_api()

@app.post("/api/fbs/activity")
def demo_existing_activity():
    return existing_wms.change_activity()

app.register_blueprint(make_synthetic_worker_scanner_blueprint(pg, get_session_employee))


def csrf_ok(data):
    return isinstance(data, dict) and bool(session.get("demo_csrf")) and secrets.compare_digest(
        str(data.get("csrf","")), session["demo_csrf"])


def is_manager():
    return session.get("manager_web") is True and manager_access() and bool(session.get("pr192_manager_id"))


app.register_blueprint(make_synthetic_printing_manager_blueprint(pg, is_manager))
# Optional dedicated token: old disposable demos stay virtual-only.
if os.environ.get('GRIT_PR192_USB_AGENT_TOKEN'):
    app.register_blueprint(make_synthetic_usb_bridge_blueprint(pg, is_manager))


def identity():
    return require_bound_terminal_identity(session, get_session_employee())


def fresh_csrf():
    if not session.get("demo_csrf"):
        session["demo_csrf"] = secrets.token_urlsafe(24)
    return session["demo_csrf"]


def result(data, status=200):
    return jsonify(data), status


@app.get("/health")
def health():
    with pg() as conn:
        conn.execute("SELECT 1")
    return jsonify(ok=True, isolation="synthetic", production=False)


@app.get("/")
def home():
    return redirect("/demo")


@app.get("/demo")
def page():
    return Response(HTML, mimetype="text/html", headers={"Cache-Control":"no-store"})


@app.get("/demo/session")
def demo_session():
    emp = identity()
    return jsonify(employee=emp[0] if emp else None,
                   manager=is_manager(), csrf=fresh_csrf(),
                   manager_csrf=session.get("pr192_manager_csrf") if is_manager() else None,
                   synthetic=True)


@app.post("/demo/login")
def demo_login():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode")
    session.clear()
    if mode == "employee" and secrets.compare_digest(str(data.get("badge","")), "DEMO-BADGE-1"):
        session.update(employee_id="demo-e1",employee_shift_id="demo-shift-1",
                       attendance_verified=True)
        return jsonify(ok=True, employee="demo-e1")
    if mode == "manager" and secrets.compare_digest(str(data.get("password","")),
                                                   os.environ["GRIT_PR192_DEMO_PASSWORD"]):
        session.update(manager_web=True, pr192_manager_id="demo-manager",
                       pr192_manager_csrf=secrets.token_urlsafe(24))
        return jsonify(ok=True, manager=True)
    return jsonify(error="Неверный демонстрационный код"), 401


@app.post("/demo/logout")
def demo_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/demo/printers")
def printers():
    if not is_manager():
        return jsonify(error="MANAGER_REQUIRED"),403
    with pg() as conn:
        rows=conn.execute("""SELECT printer_id,name,lines_json,enabled
          FROM grit_line_printers ORDER BY name""").fetchall()
    return jsonify(printers=[{"id":r[0],"name":r[1],"lines":json.loads(r[2]),
                            "enabled":bool(r[3])} for r in rows])


@app.post("/demo/printers")
def add_printer():
    if not is_manager():
        return jsonify(error="MANAGER_REQUIRED"),403
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(error="CSRF_REQUIRED"),403
    try:
        printer,qr=create_printer("demo-"+uuid.uuid4().hex[:12],
                                  data.get("name"),data.get("lines"))
        label=printable_58x40_html(printer,qr)
    except (ValueError,TypeError) as exc:
        return jsonify(error=str(exc)),400
    with pg() as conn:
        conn.execute("INSERT INTO grit_line_printers VALUES(%s,%s,%s,%s,1)",
                     (printer.printer_id,printer.name,
                      json.dumps(printer.lines,ensure_ascii=False),printer.token_digest))
    return jsonify(ok=True,printer_id=printer.printer_id,
                   label_html=label,qr=qr),201


@app.post("/demo/printers/<printer_id>/toggle")
def toggle_printer(printer_id):
    if not is_manager():
        return jsonify(error="MANAGER_REQUIRED"),403
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data) or type(data.get("enabled")) is not bool:
        return jsonify(error="INVALID_REQUEST"),400
    with pg() as conn:
        row=conn.execute("""UPDATE grit_line_printers SET enabled=%s
            WHERE printer_id=%s RETURNING printer_id""",
            (int(data["enabled"]),printer_id)).fetchone()
    return jsonify(ok=bool(row)),200 if row else 404


@app.post("/demo/printers/<printer_id>/configure")
def configure_printer(printer_id):
    if not is_manager():
        return jsonify(error="MANAGER_REQUIRED"),403
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(error="CSRF_REQUIRED"),403
    from line_printer_registry import normalize_lines
    try:
        lines=normalize_lines(data.get("lines"))
        name=data.get("name")
        if not isinstance(name,str) or not 1 <= len(name.strip()) <= 80:
            raise ValueError("INVALID_NAME")
    except (ValueError,TypeError):
        return jsonify(error="INVALID_PRINTER_CONFIG"),400
    with pg() as conn:
        row=conn.execute("""UPDATE grit_line_printers
          SET name=%s,lines_json=%s WHERE printer_id=%s RETURNING printer_id""",
          (name.strip(),json.dumps(lines,ensure_ascii=False),printer_id)).fetchone()
    return jsonify(ok=bool(row)),200 if row else 404


@app.post("/demo/printers/<printer_id>/rotate")
def rotate_printer(printer_id):
    if not is_manager():
        return jsonify(error="MANAGER_REQUIRED"),403
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(error="CSRF_REQUIRED"),403
    from line_printer_registry import Printer,rotate_printer_token
    with pg() as conn:
        row=conn.execute("""SELECT name,lines_json,token_digest,enabled
          FROM grit_line_printers WHERE printer_id=%s FOR UPDATE""",
          (printer_id,)).fetchone()
        if not row:
            return jsonify(error="PRINTER_MISSING"),404
        if row[3]!=1:
            return jsonify(error="PRINTER_DISABLED"),409
        current=Printer(printer_id,row[0],tuple(json.loads(row[1])),
                        row[2],True)
        replacement,qr=rotate_printer_token(current)
        label=printable_58x40_html(replacement,qr)
        conn.execute("""UPDATE grit_line_printers SET token_digest=%s
          WHERE printer_id=%s""",(replacement.token_digest,printer_id))
    return jsonify(ok=True,qr=qr,label_html=label)


@app.post("/demo/orders")
def orders():
    emp=identity()
    if not emp:
        return jsonify(error="EMPLOYEE_REQUIRED"),401
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(error="CSRF_REQUIRED"),403
    qr=data.get("qr","")
    from line_printer_registry import Printer,verify_printer_scan
    with pg() as conn:
        printers=conn.execute("SELECT printer_id,name,lines_json,token_digest,enabled FROM grit_line_printers").fetchall()
        active=next((p for p in printers if verify_printer_scan(qr,Printer(p[0],p[1],tuple(json.loads(p[2])),p[3],bool(p[4])))),None)
        if active is None:
            return jsonify(error="INVALID_OR_REVOKED_PRINTER_QR"),403
        available=[]
        for oid,wb,local in conn.execute("""SELECT order_id,wb_status,local_status FROM wb_fbs_orders
                    WHERE account_id='synthetic' ORDER BY order_id""").fetchall():
            state=conn.execute("""SELECT state FROM synthetic_line_printer_lifecycle
                    WHERE account_id='synthetic' AND order_id=%s""",(oid,)).fetchone()
            if state and state[0] not in ("FREE","RELEASED"):
                continue
            check=validate_locked_synthetic(
                conn, account_id="synthetic",order_id=oid,
                employee_id=emp[0],shift_id=emp[1],printer_id=active[0],qr_payload=qr)
            if check=="ELIGIBLE":
                available.append({"order_id":oid,"status":local})
    return jsonify(orders=available,printer=active[1])


@app.post("/demo/scan")
def scan():
    emp=identity()
    if not emp:
        return jsonify(error="EMPLOYEE_REQUIRED"),401
    data=request.get_json(silent=True) or {}
    if not csrf_ok(data):
        return jsonify(error="CSRF_REQUIRED"),403
    order,lease,sku=(data.get(k) for k in ("order_id","reservation_id","sku"))
    if any(not isinstance(x,str) or len(x)>160 or not x.strip() for x in (order,lease,sku)):
        return jsonify(error="INVALID_INPUT"),400
    with pg() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                     ("synthetic\x1f"+order,))
        claim=conn.execute("""SELECT state,reservation_id,employee_id,shift_id
            FROM synthetic_line_printer_lifecycle
            WHERE account_id='synthetic' AND order_id=%s FOR UPDATE""",(order,)).fetchone()
        if not claim or claim != ("RESERVED",lease,emp[0],emp[1]):
            return jsonify(error="NOT_YOUR_ACTIVE_RESERVATION"),409
        status=conn.execute("""SELECT wb_status,local_status FROM wb_fbs_orders
          WHERE account_id='synthetic' AND order_id=%s FOR UPDATE""",(order,)).fetchone()
        if status!=("confirm","pending"):
            return jsonify(error="ORDER_NOT_PENDING"),409
        if not conn.execute("""SELECT 1 FROM wb_fbs_order_skus
            WHERE account_id='synthetic' AND order_id=%s AND sku=%s""",(order,sku)).fetchone():
            return jsonify(error="WRONG_SKU"),409
        # One-unit synthetic demo orders only. Never mark multi-SKU orders complete.
        quantity=conn.execute("""SELECT count(*) FROM wb_fbs_order_skus
          WHERE account_id='synthetic' AND order_id=%s""",(order,)).fetchone()[0]
        if quantity!=1:
            return jsonify(error="MULTI_ITEM_NOT_SUPPORTED"),409
        conn.execute("""INSERT INTO scans VALUES(%s,%s,%s,%s,'WB')""",
                     (str(uuid.uuid4()),emp[1],emp[0],order))
        conn.execute("""UPDATE wb_fbs_orders SET local_status='complete'
          WHERE account_id='synthetic' AND order_id=%s""",(order,))
    return jsonify(ok=True,scanned=sku,status="complete")


@app.get("/demo/order/<order_id>/sku")
def sku_hint(order_id):
    # Synthetic barcode shown onscreen so a phone scanner is not required.
    if not identity():
        return jsonify(error="EMPLOYEE_REQUIRED"),401
    with pg() as conn:
        row=conn.execute("""SELECT sku FROM wb_fbs_order_skus
          WHERE account_id='synthetic' AND order_id=%s LIMIT 1""",(order_id,)).fetchone()
    return jsonify(sku=row[0] if row else None)


HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Маршрутизация по принтерам · ТЕСТ</title>
<style>
*{box-sizing:border-box}body{background:#0c1423;color:#eaf1fc;margin:0;font:15px system-ui}
main{max-width:1050px;margin:auto;padding:20px}h1{font-size:26px}
a{color:#9fc8ff}p{color:#a5b9d1}.panel{border:1px solid #34465e;border-radius:14px;background:#16243a;padding:18px;margin:14px 0}
input{background:#0a1525;color:#fff;border:1px solid #445974;border-radius:8px;padding:12px;max-width:450px;width:100%;margin:5px 0}
button{border:0;border-radius:9px;background:#326ec7;color:white;padding:11px 15px;font-weight:700;margin:5px;cursor:pointer}
button.secondary{background:#30435d}button:disabled{opacity:.4}
.row{display:flex;gap:15px;flex-wrap:wrap}.row>*{flex:1;min-width:245px}
#output{white-space:pre-wrap;background:#091525;padding:15px;border-radius:9px;min-height:45px;overflow-wrap:anywhere}
small{color:#93a7bf}.item{border-top:1px solid #34465e;padding:12px 0}
</style></head><body><main><h1>GRIT WMS · Принтеры по линиям</h1>
<p><a href="/demo/worker-qr">🧪 Экран сборщика · QR принтера</a> · <a href="/demo/printing">📋 Руководитель · Очередь и виртуальная печать</a></p>
<p>ИЗОЛИРОВАННОЕ ДЕМО. Только синтетические WB-заказы. Не подключено к реальным принтерам и складу.</p>
<div class="row"><section class="panel"><h3>Вход по демо-бейджу</h3>
<input id="badge" value="DEMO-BADGE-1"><button onclick="login('employee')">Войти как сборщик</button></section>
<section class="panel"><h3>Вход руководителя</h3>
<input id="pwd" type="password" placeholder="Пароль демо"><button onclick="login('manager')">Войти как руководитель</button></section></div>
<section class="panel"><h3>Моя сессия</h3><div id="who"></div>
<button class="secondary" onclick="logout()">Выйти</button></section>
<section class="panel"><h3>Руководитель · Принтеры и QR 58×40</h3>
<input id="printerName" placeholder="Принтер А-Б" value="Принтер А-Б">
<input id="printerLines" value="А, Б" placeholder="А, Б">
<button onclick="createPrinter()">Создать принтер и получить QR</button>
<p><small>У существующего принтера можно изменить название и линии либо перевыпустить QR.</small></p>
<div id="printers"></div></section>
<section class="panel"><h3>Сотрудник · QR принтера → заказы</h3>
<input id="qr" placeholder="Отсканируй QR принтера">
<button onclick="findOrders()">Показать подходящие заказы</button><div id="orders"></div></section>
<section class="panel"><h3>Сканирование и завершение</h3>
<input id="order" placeholder="ID заказа" readonly>
<input id="lease" placeholder="Резервирование" readonly>
<input id="sku" placeholder="Штрихкод товара SYNTH-SKU-A">
<button onclick="doScan()">Подтвердить скан синтетического товара</button>
<button onclick="complete()">Завершить заказ</button></section>
<section class="panel"><h3>Руководитель · Освобождение и аудит</h3>
<input id="releaseOrder" placeholder="ID заказа">
<input id="releaseLease" placeholder="ID резервирования">
<input id="reason" value="Руководитель проверил недостачу" placeholder="Причина">
<button onclick="release()">Освободить</button>
<button class="secondary" onclick="audit()">Журнал заказа</button></section>
<section class="panel"><h3>Результат</h3><div id="output"></div></section>
<script>
let csrf='',mgrcsrf='',activeQR='';
const el=id=>document.getElementById(id),esc=s=>String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
function info(v){el('output').textContent=typeof v==='string'?v:JSON.stringify(v,null,2)}
async function api(path,method='GET',data){
 const opts={method,credentials:'same-origin',headers:method==='GET'?{}:{'Content-Type':'application/json'}};
 if(data)opts.body=JSON.stringify(data);
 const r=await fetch(path,opts),j=await r.json();
 if(!r.ok)throw Error(JSON.stringify(j));return j
}
async function wrap(fn){try{await fn()}catch(e){info(e.message)}}
async function refresh(){
 const s=await api('/demo/session');csrf=s.csrf;el('who').textContent='Сотрудник: '+(s.employee||'нет')+' · Руководитель: '+(s.manager?'да':'нет');
 if(s.manager){
 const j=await api('/demo/printers');
 el('printers').innerHTML=j.printers.map(p=>`<div class="item"><b>${esc(p.name)}</b> · ${esc(p.lines.join(', '))} <small>${p.enabled?'ВКЛ':'ВЫКЛ'}</small> <button class="secondary printer-toggle" data-id="${esc(p.id)}" data-enabled="${p.enabled?'0':'1'}">Переключить</button> <button class="secondary printer-config" data-id="${esc(p.id)}" data-name="${esc(p.name)}" data-lines="${esc(p.lines.join(', '))}">Линии</button> <button class="secondary printer-rotate" data-id="${esc(p.id)}">Новый QR</button></div>`).join('');
 el('printers').querySelectorAll('.printer-toggle').forEach(b=>b.addEventListener('click',()=>toggle(b.dataset.id,b.dataset.enabled==='1')));
 el('printers').querySelectorAll('.printer-config').forEach(b=>b.addEventListener('click',()=>configure(b.dataset.id,b.dataset.name,b.dataset.lines)));
 el('printers').querySelectorAll('.printer-rotate').forEach(b=>b.addEventListener('click',()=>rotate(b.dataset.id)));
 }else el('printers').textContent='Требуется вход руководителя';
}
async function login(mode){return wrap(async()=>{const j=await api('/demo/login','POST',{mode,badge:el('badge').value,password:el('pwd').value});info(j);await refresh()})}
async function logout(){return wrap(async()=>{await api('/demo/logout','POST');location.reload()})}
async function toggle(id,enabled){return wrap(async()=>{info(await api('/demo/printers/'+encodeURIComponent(id)+'/toggle','POST',{csrf,enabled}));await refresh()})}
function saveLabel(j){
 el('qr').value=j.qr;activeQR=j.qr;
 const blob=new Blob([j.label_html],{type:'text/html'}),url=URL.createObjectURL(blob);
 const a=document.createElement('a');a.href=url;a.download='GRIT-QR-58x40.html';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function configure(id,oldName,oldLines){return wrap(async()=>{
 const name=prompt('Название принтера',oldName);if(name===null)return;
 const lines=prompt('Линии через запятую',oldLines);if(lines===null)return;
 info(await api('/demo/printers/'+encodeURIComponent(id)+'/configure','POST',{csrf,name,lines:lines.split(',').map(x=>x.trim())}));await refresh()
})}
async function rotate(id){return wrap(async()=>{
 if(!confirm('Старый QR перестанет работать. Перевыпустить?'))return;
 const j=await api('/demo/printers/'+encodeURIComponent(id)+'/rotate','POST',{csrf});
 saveLabel(j);info('QR перевыпущен. Старый отозван. Этикетка сохранена.');await refresh()
})}
async function createPrinter(){return wrap(async()=>{
 const j=await api('/demo/printers','POST',{csrf,name:el('printerName').value,lines:el('printerLines').value.split(',').map(x=>x.trim())});
 saveLabel(j);
 info('Демо-принтер создан. Этикетка 58×40 получена. QR введён в поле сотрудника.');await refresh()
})}
async function findOrders(){return wrap(async()=>{
 activeQR=el('qr').value;
 const j=await api('/demo/orders','POST',{csrf,qr:activeQR});
 el('orders').innerHTML='<p>'+esc(j.printer)+'</p>'+j.orders.map(o=>`<div class="item"><b>${esc(o.order_id)}</b> <button class="order-reserve" data-order="${esc(o.order_id)}">Забрать заказ</button></div>`).join('');
 el('orders').querySelectorAll('.order-reserve').forEach(b=>b.addEventListener('click',()=>reserve(b.dataset.order)));
 info(j)
})}
async function reserve(order){return wrap(async()=>{
 const lease=crypto.randomUUID(),action=crypto.randomUUID();
 const j=await api('/synthetic/pr192/reserve','POST',{order_id:order,printer_qr:activeQR,reservation_id:lease,action_id:action});
 el('order').value=order;el('lease').value=lease;el('releaseOrder').value=order;el('releaseLease').value=lease;
 el('sku').value=(await api('/demo/order/'+encodeURIComponent(order)+'/sku')).sku;
 info(j)
})}
async function doScan(){return wrap(async()=>info(await api('/demo/scan','POST',{csrf,order_id:el('order').value,reservation_id:el('lease').value,sku:el('sku').value})))}
async function complete(){return wrap(async()=>info(await api('/synthetic/pr192/complete','POST',{order_id:el('order').value,reservation_id:el('lease').value,action_id:crypto.randomUUID()})))}
async function release(){return wrap(async()=>{
 const session=await api('/demo/session');
 // Separate CSRF token for privileged manager operation is obtained via manager session bootstrap.
 info(await api('/synthetic/pr192/release','POST',{order_id:el('releaseOrder').value,reservation_id:el('releaseLease').value,reason:el('reason').value,action_id:crypto.randomUUID(),csrf:session.manager_csrf}))
})}
async function audit(){return wrap(async()=>info(await api('/synthetic/pr192/audit/'+encodeURIComponent(el('releaseOrder').value))))}
refresh().catch(e=>info(e.message));
</script></main></body></html>"""
