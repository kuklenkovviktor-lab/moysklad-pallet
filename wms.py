import os
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request

wms_bp = Blueprint("wms", __name__)

RATE = float(os.getenv("FBS_RATE", "2.25"))
TARGET = int(os.getenv("FBS_TARGET", "1600"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("WMS_DB_PATH", "/tmp/wms.db")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def db_connect():
    if DATABASE_URL:
        import psycopg
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        return psycopg.connect(url)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    return conn


def sql(query):
    return query.replace("?", "%s") if DATABASE_URL else query


def query_all(query, params=()):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql(query), params)
        rows = cur.fetchall()
        cur.close()
        return rows


def query_one(query, params=()):
    rows = query_all(query, params)
    return rows[0] if rows else None


def execute(query, params=()):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql(query), params)
        conn.commit()
        cur.close()


def init_wms_db():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS employees (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            rate REAL NOT NULL DEFAULT 2.25,
            target INTEGER NOT NULL DEFAULT 1600,
            active INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS shifts (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS scans (
            id TEXT PRIMARY KEY,
            shift_id TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            barcode TEXT NOT NULL,
            scanned_at TEXT NOT NULL,
            marketplace TEXT NOT NULL DEFAULT 'WB',
            UNIQUE(shift_id, barcode),
            FOREIGN KEY(shift_id) REFERENCES shifts(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS activities (
            id TEXT PRIMARY KEY,
            shift_id TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            activity_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            FOREIGN KEY(shift_id) REFERENCES shifts(id),
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """,
    ]
    with db_connect() as conn:
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)
        conn.commit()
        cur.close()

    for emp_id, name in [("ilya", "Илья"), ("anton", "Антон")]:
        if not query_one("SELECT id FROM employees WHERE id = ?", (emp_id,)):
            execute(
                "INSERT INTO employees (id, name, rate, target, active) VALUES (?, ?, ?, ?, 1)",
                (emp_id, name, RATE, TARGET),
            )


def ensure_db():
    init_wms_db()


def current_activity(shift_id):
    row = query_one(
        """
        SELECT id, activity_type, started_at
        FROM activities
        WHERE shift_id = ? AND ended_at IS NULL
        ORDER BY started_at DESC
        LIMIT 1
        """,
        (shift_id,),
    )
    if not row:
        return None
    return {"id": row[0], "type": row[1], "started_at": row[2]}


def picking_seconds(shift_id, now=None):
    now = now or datetime.now(timezone.utc)
    rows = query_all(
        """
        SELECT started_at, ended_at
        FROM activities
        WHERE shift_id = ? AND activity_type = 'picking'
        """,
        (shift_id,),
    )
    total = 0.0
    for started_at, ended_at in rows:
        start = parse_iso(started_at)
        end = parse_iso(ended_at) if ended_at else now
        if start and end and end >= start:
            total += (end - start).total_seconds()
    return max(total, 0.0)


def shift_stats(shift_id):
    shift = query_one(
        """
        SELECT s.id, s.employee_id, e.name, s.started_at, s.ended_at, e.rate, e.target
        FROM shifts s
        JOIN employees e ON e.id = s.employee_id
        WHERE s.id = ?
        """,
        (shift_id,),
    )
    if not shift:
        return None

    count_row = query_one("SELECT COUNT(*) FROM scans WHERE shift_id = ?", (shift_id,))
    orders = int(count_row[0] if count_row else 0)
    pick_sec = picking_seconds(shift_id)
    pick_hours = pick_sec / 3600.0
    speed = round(orders / pick_hours, 1) if pick_hours > 0 else 0.0
    rate = float(shift[5])
    target = int(shift[6])
    earned = round(orders * rate, 2)
    active = current_activity(shift_id)

    return {
        "shift_id": shift[0],
        "employee_id": shift[1],
        "employee": shift[2],
        "started_at": shift[3],
        "ended_at": shift[4],
        "orders": orders,
        "target": target,
        "remaining": max(target - orders, 0),
        "percent": round((orders / target * 100.0) if target else 0.0, 1),
        "rate": rate,
        "earned": earned,
        "orders_per_hour": speed,
        "picking_seconds": int(pick_sec),
        "activity": active["type"] if active else None,
        "target_done": orders >= target,
    }


FBS_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3">
<title>FBS · Выработка</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
:root{--bg:#e9eef3;--panel:#f7f9fb;--panel2:#eef3f6;--text:#25313d;--muted:#6b7785;--line:#d7e0e7;--accent:#48647a;--accent2:#dde8ef}
*{box-sizing:border-box}html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text)}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100dvh;-webkit-text-size-adjust:100%}
.wrap{width:100%;max-width:980px;margin:auto;padding:max(10px,env(safe-area-inset-top)) max(10px,env(safe-area-inset-right)) max(10px,env(safe-area-inset-bottom)) max(10px,env(safe-area-inset-left))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:15px;padding:clamp(11px,2vw,17px);box-shadow:0 2px 10px rgba(52,72,91,.07);margin-bottom:9px}
h1{font-size:clamp(20px,3vw,25px);margin:0 0 5px}h2{font-size:clamp(15px,2.2vw,18px);margin:0 0 7px}
.muted,.small{color:var(--muted)}.muted{font-size:clamp(12px,1.8vw,14px)}.small{font-size:clamp(10px,1.5vw,12px)}
select,input{width:100%;padding:clamp(9px,1.6vw,13px);font-size:clamp(14px,2vw,16px);color:var(--text);border:1px solid var(--line);border-radius:10px;background:#fbfcfd;outline:none}
button{width:100%;padding:clamp(9px,1.6vw,13px);font-size:clamp(13px,2vw,15px);font-weight:750;border:1px solid transparent;border-radius:10px;cursor:pointer;background:var(--accent);color:#f7fafc;margin-top:7px}
button.secondary{background:var(--accent2);color:var(--text);border-color:#cbd9e2}button.active{background:#d3e3ed;color:#243b4c;border-color:#8ca7b9;box-shadow:inset 0 0 0 1px #8ca7b9}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:7px}.stat{background:var(--panel2);border:1px solid #dde5eb;border-radius:11px;padding:9px}.big{font-size:clamp(22px,4vw,30px);font-weight:820;line-height:1.05}
.ok,.err{padding:9px;border-radius:9px;margin-top:7px;font-size:12px}.ok{background:#e1f1e6;color:#2f6b45}.err{background:#f6e4e4;color:#8a4343}
#reader{margin-top:7px;overflow:hidden;border-radius:11px;max-height:42dvh}.hidden{display:none!important}
.progress{height:9px;background:#dbe3e9;border-radius:999px;overflow:hidden;margin:6px 0}.progress>div{height:100%;background:#6f8ca1;width:0%}
.activities{display:grid;grid-template-columns:1fr 1fr;gap:5px}.activities button{margin:0;padding:9px;font-size:12px}
a{color:#536f83;text-decoration:none;font-weight:650}
.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px}
.nav a{display:flex;align-items:center;justify-content:center;text-align:center;min-height:38px;padding:7px 6px;border-radius:9px;background:#dde8ef;color:#314657;border:1px solid #cbd9e2;font-size:12px;font-weight:750}
.nav a.active{background:#48647a;color:#fff;border-color:#48647a}
@media (orientation:landscape) and (max-height:560px){
body{overflow:hidden}.wrap{max-width:none;height:100dvh;overflow:hidden;padding:max(5px,env(safe-area-inset-top)) max(8px,env(safe-area-inset-right)) max(5px,env(safe-area-inset-bottom)) max(8px,env(safe-area-inset-left))}
.wrap>.nav + .card{height:42px;display:flex;align-items:center;gap:8px;padding:5px 9px;margin-bottom:5px;border-radius:10px}.wrap>.nav + .card h1{font-size:16px;margin:0;white-space:nowrap}.wrap>.nav + .card .muted{display:none}.wrap>.nav + .card div:last-child{margin:0!important;margin-left:auto!important;font-size:11px;white-space:nowrap}
#startCard{max-width:540px;margin:5px auto;padding:11px}
#work{display:grid!important;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr) minmax(0,.95fr);gap:5px;height:calc(100dvh - 52px - env(safe-area-inset-top) - env(safe-area-inset-bottom));align-items:stretch}
#work>.card{min-width:0;height:100%;margin:0;padding:8px;border-radius:10px;overflow:auto;box-shadow:none}
#work h2{font-size:13px;margin-bottom:4px}#work .big{font-size:clamp(19px,3vw,25px)}#work .small{font-size:9px}#work .stat{padding:6px}
#work button{padding:6px 5px;font-size:11px;margin-top:4px;border-radius:8px}#work input{padding:7px;font-size:12px;border-radius:8px}
#reader{margin-top:4px;max-height:135px}.activities{grid-template-columns:1fr 1fr;gap:3px}.activities button{padding:5px 3px;font-size:10px;min-height:28px}.progress{margin:4px 0;height:7px}.ok,.err{padding:6px;margin-top:4px;font-size:10px}
}
@media (orientation:landscape) and (max-width:760px){body{overflow:auto}.wrap{height:auto;overflow:visible}#work{grid-template-columns:1fr 1fr;height:auto}#work>.card{height:auto;overflow:visible}#work>.card:last-child{grid-column:1/-1}.activities{grid-template-columns:repeat(4,1fr)}}
</style>
</head>
<body>
<div class="wrap">
<div class="nav">
<a href="/">📦 Приёмка</a>
<a class="active" href="/fbs">🚀 FBS</a>
<a href="/fbs-manager">📊 Руководитель</a>
</div>
<div class="card">
<h1>🚀 FBS · Выработка</h1>
<div class="muted">Скан заказа фиксирует сотрудника, время, выработку и оплату. Дубли не засчитываются.</div>
</div>

<div class="card" id="startCard">
<h2>Сотрудник</h2>
<select id="employee"><option>Загрузка…</option></select>
<button onclick="startShift()">Начать смену</button>
</div>

<div id="work" class="hidden">
<div class="card">
<div class="small" id="employeeName">Сотрудник</div>
<div class="big"><span id="orders">0</span> / <span id="target">1600</span></div>
<div class="progress"><div id="bar"></div></div>
<div class="grid">
<div class="stat"><div class="small">Заработано</div><div class="big" id="money">0 ₽</div></div>
<div class="stat"><div class="small">Заказов/час</div><div class="big" id="speed">0</div></div>
</div>
<div class="small" style="margin-top:10px" id="remaining"></div>
<div id="result"></div>
</div>

<div class="card">
<h2>Скан заказа WB</h2>
<button onclick="toggleCamera()">📷 Камера телефона</button>
<div id="reader" class="hidden"></div>
<input id="barcode" autocomplete="off" inputmode="text" placeholder="ШК заказа / сканер" style="margin-top:10px">
<button class="secondary" onclick="manualScan()">Засчитать заказ</button>
</div>

<div class="card">
<h2>Текущая операция</h2>
<div class="activities" id="activities">
<button data-a="picking" onclick="activity('picking')">Сборка</button>
<button data-a="replenishment" onclick="activity('replenishment')">Пополнение</button>
<button data-a="receiving" onclick="activity('receiving')">Приёмка</button>
<button data-a="shipping" onclick="activity('shipping')">Отгрузка</button>
<button data-a="pallets" onclick="activity('pallets')">Палеты</button>
<button data-a="kgt" onclick="activity('kgt')">КГТ</button>
<button data-a="downtime" onclick="activity('downtime')">Простой</button>
<button data-a="other" onclick="activity('other')">Другое</button>
</div>
<button class="secondary" onclick="endShift()">Завершить смену</button>
</div>
</div>
</div>
<script>
let shiftId=localStorage.getItem('fbs_shift_id')||'';
let scanner=null,busy=false,audioCtx=null;

function beep(ok=true){
 try{
  const C=window.AudioContext||window.webkitAudioContext;
  if(!audioCtx&&C)audioCtx=new C();
  if(!audioCtx)return;
  if(audioCtx.state==='suspended')audioCtx.resume();
  const o=audioCtx.createOscillator(),g=audioCtx.createGain();
  o.frequency.value=ok?1100:220;o.type=ok?'sine':'square';
  g.gain.value=.12;o.connect(g);g.connect(audioCtx.destination);o.start();o.stop(audioCtx.currentTime+.12);
  if(navigator.vibrate)navigator.vibrate(ok?80:[160,70,160]);
 }catch(e){}
}
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}

async function loadEmployees(){
 const r=await fetch('/api/fbs/employees'),d=await r.json();
 const el=document.getElementById('employee');el.innerHTML='';
 d.employees.forEach(x=>{const o=document.createElement('option');o.value=x.id;o.textContent=x.name;el.appendChild(o)});
 if(shiftId)await refresh();
}
async function startShift(){
 const employee_id=document.getElementById('employee').value;
 const r=await fetch('/api/fbs/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({employee_id})});
 const d=await r.json();if(!r.ok){showErr(d.error);return}
 shiftId=d.shift_id;localStorage.setItem('fbs_shift_id',shiftId);await refresh();
}
async function refresh(){
 if(!shiftId)return;
 const r=await fetch('/api/fbs/stats?shift_id='+encodeURIComponent(shiftId)),d=await r.json();
 if(!r.ok){localStorage.removeItem('fbs_shift_id');shiftId='';return}
 document.getElementById('startCard').classList.add('hidden');document.getElementById('work').classList.remove('hidden');
 document.getElementById('employeeName').textContent=d.employee;
 document.getElementById('orders').textContent=d.orders;document.getElementById('target').textContent=d.target;
 document.getElementById('money').textContent=d.earned.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽';
 document.getElementById('speed').textContent=d.orders_per_hour;
 document.getElementById('bar').style.width=Math.min(d.percent,100)+'%';
 document.getElementById('remaining').textContent=d.target_done?'✅ Минималка выполнена':'До 1600: '+d.remaining;
 document.querySelectorAll('[data-a]').forEach(b=>b.classList.toggle('active',b.dataset.a===d.activity));
}
async function sendScan(code){
 code=(code||'').trim();if(!code||busy||!shiftId)return;busy=true;
 try{
  const r=await fetch('/api/fbs/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId,barcode:code})});
  const d=await r.json();
  if(!r.ok){beep(false);document.getElementById('result').innerHTML='<div class="err">'+esc(d.error||'Ошибка')+'</div>';return}
  beep(true);document.getElementById('result').innerHTML='<div class="ok">✅ +1 заказ · '+esc(code)+'</div>';await refresh();
 }finally{setTimeout(()=>busy=false,250)}
}
function manualScan(){const el=document.getElementById('barcode');const v=el.value;el.value='';el.focus();sendScan(v)}
document.getElementById('barcode').addEventListener('keydown',e=>{if(e.key==='Enter')manualScan()});

async function activity(type){
 const r=await fetch('/api/fbs/activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId,activity_type:type})});
 const d=await r.json();if(!r.ok){showErr(d.error);return}await refresh();
}
async function endShift(){
 if(!confirm('Завершить смену?'))return;
 const r=await fetch('/api/fbs/end',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId})});
 const d=await r.json();if(!r.ok){showErr(d.error);return}
 localStorage.removeItem('fbs_shift_id');shiftId='';location.reload();
}
async function toggleCamera(){
 if(scanner){try{await scanner.stop()}catch(e){}scanner=null;document.getElementById('reader').classList.add('hidden');return}
 if(typeof Html5Qrcode==='undefined'){showErr('Модуль камеры не загрузился');return}
 document.getElementById('reader').classList.remove('hidden');scanner=new Html5Qrcode('reader');
 try{
  await scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:320,height:160}},txt=>sendScan(txt),()=>{});
 }catch(e){scanner=null;showErr('Камера не открылась: '+e)}
}
function showErr(m){beep(false);document.getElementById('result').innerHTML='<div class="err">'+esc(m)+'</div>'}
loadEmployees();setInterval(()=>{if(shiftId)refresh()},15000);
</script></body></html>"""


MANAGER_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FBS · Руководитель</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f5f6f8;margin:0;color:#111}
.wrap{max-width:900px;margin:auto;padding:16px}.card{background:#fff;border-radius:18px;padding:18px;margin-bottom:14px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{margin:0 0 8px}.worker{display:grid;grid-template-columns:1.3fr repeat(4,1fr);gap:8px;padding:12px 0;border-bottom:1px solid #eee}.worker:last-child{border:0}
.big{font-size:28px;font-weight:850}.small{font-size:12px;color:#666}
.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.nav a{display:flex;align-items:center;justify-content:center;text-align:center;min-height:42px;padding:8px;border-radius:11px;text-decoration:none;font-weight:700;font-size:13px;background:#e7edf2;color:#314657;border:1px solid #d2dde5}
.nav a.active{background:#48647a;color:#fff;border-color:#48647a}
@media(max-width:650px){.worker{grid-template-columns:1fr 1fr}.name{grid-column:1/-1}}
</style></head><body><div class="wrap">
<div class="nav"><a href="/">📦 Приёмка</a><a href="/fbs">🚀 FBS</a><a class="active" href="/fbs-manager">📊 Руководитель</a></div>
<div class="card"><h1>📊 FBS · Руководитель</h1></div>
<div class="card"><div class="small">Активные смены</div><div class="big" id="active">0</div></div>
<div class="card" id="workers">Загрузка…</div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
async function load(){
 const r=await fetch('/api/fbs/dashboard'),d=await r.json();document.getElementById('active').textContent=d.workers.length;
 const box=document.getElementById('workers');
 if(!d.workers.length){box.innerHTML='Нет активных смен';return}
 box.innerHTML=d.workers.map(x=>'<div class="worker"><div class="name"><b>'+esc(x.employee)+'</b><div class="small">'+esc(x.activity||'')+'</div></div><div><b>'+x.orders+'</b><div class="small">заказов</div></div><div><b>'+x.orders_per_hour+'</b><div class="small">заказ/ч</div></div><div><b>'+x.earned.toLocaleString('ru-RU')+' ₽</b><div class="small">заработано</div></div><div><b>'+x.percent+'%</b><div class="small">от 1600</div></div></div>').join('');
}
load();setInterval(load,10000);
</script></body></html>"""


@wms_bp.get("/fbs")
def fbs_page():
    ensure_db()
    return FBS_PAGE


@wms_bp.get("/fbs-manager")
def manager_page():
    ensure_db()
    return MANAGER_PAGE


@wms_bp.get("/api/fbs/employees")
def employees():
    ensure_db()
    rows = query_all("SELECT id, name, rate, target FROM employees WHERE active = 1 ORDER BY name")
    return jsonify(employees=[
        {"id": r[0], "name": r[1], "rate": float(r[2]), "target": int(r[3])}
        for r in rows
    ])


@wms_bp.post("/api/fbs/start")
def start_shift():
    ensure_db()
    data = request.get_json(force=True)
    employee_id = str(data.get("employee_id", "")).strip()
    emp = query_one("SELECT id, name FROM employees WHERE id = ? AND active = 1", (employee_id,))
    if not emp:
        return jsonify(error="Сотрудник не найден"), 404

    existing = query_one(
        "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (employee_id,),
    )
    if existing:
        return jsonify(ok=True, shift_id=existing[0], resumed=True)

    shift_id = str(uuid.uuid4())
    ts = now_iso()
    execute(
        "INSERT INTO shifts (id, employee_id, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        (shift_id, employee_id, ts),
    )
    execute(
        "INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at, ended_at) VALUES (?, ?, ?, 'picking', ?, NULL)",
        (str(uuid.uuid4()), shift_id, employee_id, ts),
    )
    return jsonify(ok=True, shift_id=shift_id, resumed=False)


@wms_bp.post("/api/fbs/scan")
def fbs_scan():
    ensure_db()
    data = request.get_json(force=True)
    shift_id = str(data.get("shift_id", "")).strip()
    barcode = str(data.get("barcode", "")).strip()
    if not barcode:
        return jsonify(error="Пустой штрихкод"), 400

    shift = query_one(
        "SELECT employee_id, ended_at FROM shifts WHERE id = ?",
        (shift_id,),
    )
    if not shift:
        return jsonify(error="Смена не найдена"), 404
    if shift[1]:
        return jsonify(error="Смена уже завершена"), 409

    active = current_activity(shift_id)
    if not active or active["type"] != "picking":
        return jsonify(error="Сейчас включена не сборка. Переключи операцию на «Сборка»."), 409

    if query_one("SELECT id FROM scans WHERE shift_id = ? AND barcode = ?", (shift_id, barcode)):
        return jsonify(error="Этот заказ уже отсканирован в текущей смене"), 409

    try:
        execute(
            "INSERT INTO scans (id, shift_id, employee_id, barcode, scanned_at, marketplace) VALUES (?, ?, ?, ?, ?, 'WB')",
            (str(uuid.uuid4()), shift_id, shift[0], barcode, now_iso()),
        )
    except Exception:
        if query_one("SELECT id FROM scans WHERE shift_id = ? AND barcode = ?", (shift_id, barcode)):
            return jsonify(error="Этот заказ уже отсканирован в текущей смене"), 409
        raise

    stats = shift_stats(shift_id)
    return jsonify(ok=True, **stats)


@wms_bp.post("/api/fbs/activity")
def change_activity():
    ensure_db()
    data = request.get_json(force=True)
    shift_id = str(data.get("shift_id", "")).strip()
    activity_type = str(data.get("activity_type", "")).strip()
    allowed = {"picking", "replenishment", "receiving", "shipping", "pallets", "kgt", "downtime", "other"}
    if activity_type not in allowed:
        return jsonify(error="Неизвестная операция"), 400

    shift = query_one("SELECT employee_id, ended_at FROM shifts WHERE id = ?", (shift_id,))
    if not shift or shift[1]:
        return jsonify(error="Активная смена не найдена"), 404

    current = current_activity(shift_id)
    if current and current["type"] == activity_type:
        return jsonify(ok=True, unchanged=True)

    ts = now_iso()
    execute("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL", (ts, shift_id))
    execute(
        "INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at, ended_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (str(uuid.uuid4()), shift_id, shift[0], activity_type, ts),
    )
    return jsonify(ok=True)


@wms_bp.post("/api/fbs/end")
def end_shift():
    ensure_db()
    data = request.get_json(force=True)
    shift_id = str(data.get("shift_id", "")).strip()
    shift = query_one("SELECT id, ended_at FROM shifts WHERE id = ?", (shift_id,))
    if not shift:
        return jsonify(error="Смена не найдена"), 404
    if shift[1]:
        return jsonify(ok=True, already_ended=True)

    ts = now_iso()
    execute("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL", (ts, shift_id))
    execute("UPDATE shifts SET ended_at = ? WHERE id = ?", (ts, shift_id))
    return jsonify(ok=True, stats=shift_stats(shift_id))


@wms_bp.get("/api/fbs/stats")
def stats():
    ensure_db()
    shift_id = str(request.args.get("shift_id", "")).strip()
    result = shift_stats(shift_id)
    if not result:
        return jsonify(error="Смена не найдена"), 404
    return jsonify(**result)


@wms_bp.get("/api/fbs/dashboard")
def dashboard():
    ensure_db()
    rows = query_all(
        """
        SELECT s.id
        FROM shifts s
        WHERE s.ended_at IS NULL
        ORDER BY s.started_at
        """
    )
    workers = []
    for row in rows:
        item = shift_stats(row[0])
        if item:
            workers.append(item)
    return jsonify(workers=workers, target=TARGET, rate=RATE)
