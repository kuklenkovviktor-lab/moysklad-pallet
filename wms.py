import os
import sqlite3
import uuid
import hashlib
import hmac
import time
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import Blueprint, jsonify, request, session, redirect

wms_bp = Blueprint("wms", __name__)

RATE = float(os.getenv("FBS_RATE", "2.25"))
TARGET = int(os.getenv("FBS_TARGET", "1600"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("WMS_DB_PATH", "/tmp/wms.db")
REQUIRE_POSTGRES = os.getenv("WMS_REQUIRE_POSTGRES", "0").strip().lower() in ("1", "true", "yes", "on")
WMS_BADGE_SECRET = os.getenv("WMS_BADGE_SECRET", "mini-wms-badge-secret-v1").encode("utf-8")
SUPERVISOR_QR_PERIOD = max(10, int(os.getenv("SUPERVISOR_QR_PERIOD", "30")))
WMS_TERMINAL_SECRET = os.getenv("WMS_TERMINAL_SECRET", "mini-wms-terminal-secret-v1").encode("utf-8")
SUPERVISOR_TERMINAL_VERSION = os.getenv("SUPERVISOR_TERMINAL_VERSION", "1").strip() or "1"


def make_token(purpose):
    digest = hmac.new(WMS_BADGE_SECRET, purpose.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:32]


def make_badge_code(employee_id):
    return "WMS-BADGE-" + make_token("employee:" + employee_id)


def supervisor_exit_code_for_step(step):
    return "WMS-EXIT-" + make_token("supervisor-exit:" + str(step))


def current_supervisor_exit_code(now=None):
    ts = float(now if now is not None else time.time())
    step = int(ts // SUPERVISOR_QR_PERIOD)
    remaining = SUPERVISOR_QR_PERIOD - int(ts % SUPERVISOR_QR_PERIOD)
    return supervisor_exit_code_for_step(step), step, remaining


def valid_supervisor_exit_code(code, now=None):
    candidate = str(code or "").strip()
    if not candidate:
        return False
    ts = float(now if now is not None else time.time())
    step = int(ts // SUPERVISOR_QR_PERIOD)
    # Текущий и предыдущий интервал: скан на границе обновления не должен отваливаться.
    for allowed_step in (step, step - 1):
        if hmac.compare_digest(candidate, supervisor_exit_code_for_step(allowed_step)):
            return True
    return False


def issue_supervisor_terminal_token():
    nonce = uuid.uuid4().hex
    body = SUPERVISOR_TERMINAL_VERSION + "." + nonce
    digest = hmac.new(WMS_TERMINAL_SECRET, ("supervisor-terminal:" + body).encode("utf-8"), hashlib.sha256).hexdigest()
    return "WMS-TERMINAL-" + body + "." + digest[:40]


def valid_supervisor_terminal_token(token):
    candidate = str(token or "").strip()
    prefix = "WMS-TERMINAL-"
    if not candidate.startswith(prefix):
        return False
    raw = candidate[len(prefix):]
    parts = raw.split(".")
    if len(parts) != 3:
        return False
    version, nonce, supplied = parts
    if version != SUPERVISOR_TERMINAL_VERSION or not nonce:
        return False
    body = version + "." + nonce
    expected = hmac.new(WMS_TERMINAL_SECRET, ("supervisor-terminal:" + body).encode("utf-8"), hashlib.sha256).hexdigest()[:40]
    return hmac.compare_digest(supplied, expected)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


_PG_POOL = None


def _postgres_url():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _get_pg_pool():
    global _PG_POOL
    if _PG_POOL is None:
        from psycopg_pool import ConnectionPool
        _PG_POOL = ConnectionPool(
            conninfo=_postgres_url(),
            min_size=1,
            max_size=5,
            timeout=10,
            kwargs={"connect_timeout": 5},
            open=True,
        )
    return _PG_POOL


@contextmanager
def db_connect():
    if DATABASE_URL:
        pool = _get_pg_pool()
        with pool.connection() as conn:
            yield conn
        return

    if REQUIRE_POSTGRES:
        raise RuntimeError("WMS_REQUIRE_POSTGRES включён, но DATABASE_URL не задан.")

    conn = sqlite3.connect(DB_PATH, timeout=15)
    try:
        yield conn
    finally:
        conn.close()


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
            active INTEGER NOT NULL DEFAULT 1,
            role TEXT NOT NULL DEFAULT 'staff',
            badge_number INTEGER,
            badge_code TEXT
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
        """
        CREATE TABLE IF NOT EXISTS operation_events (
            id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            shift_id TEXT,
            operation_type TEXT NOT NULL,
            event_type TEXT NOT NULL,
            object_code TEXT,
            quantity REAL,
            result TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(employee_id) REFERENCES employees(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS external_pallets (
            sscc TEXT PRIMARY KEY,
            partner TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            employee_id TEXT,
            product_barcode TEXT,
            article TEXT,
            quantity REAL NOT NULL,
            accepted_at TEXT NOT NULL
        )
        """,
    ]
    with db_connect() as conn:
        cur = conn.cursor()
        for stmt in statements:
            cur.execute(stmt)

        # Миграция старой таблицы employees без потери данных.
        if DATABASE_URL:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'employees'")
            cols = {r[0] for r in cur.fetchall()}
        else:
            cur.execute("PRAGMA table_info(employees)")
            cols = {r[1] for r in cur.fetchall()}

        additions = [
            ("role", "TEXT NOT NULL DEFAULT 'staff'"),
            ("badge_number", "INTEGER"),
            ("badge_code", "TEXT"),
        ]
        for name, ddl in additions:
            if name not in cols:
                cur.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")

        conn.commit()
        cur.close()

    seeds = [
        ("viktor", "Виктор", "manager", 1),
        ("anton", "Антон", "staff", 2),
        ("ilya", "Илья", "staff", 3),
        ("loader", "Грузчик", "staff", 4),
    ]
    for emp_id, name, role, badge_number in seeds:
        badge_code = make_badge_code(emp_id)
        if query_one("SELECT id FROM employees WHERE id = ?", (emp_id,)):
            execute(
                "UPDATE employees SET name = ?, role = ?, badge_number = ?, badge_code = ?, rate = ?, target = ?, active = 1 WHERE id = ?",
                (name, role, badge_number, badge_code, RATE, TARGET, emp_id),
            )
        else:
            execute(
                "INSERT INTO employees (id, name, rate, target, active, role, badge_number, badge_code) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (emp_id, name, RATE, TARGET, role, badge_number, badge_code),
            )

    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_badge_code ON employees(badge_code)")
    except Exception:
        pass
    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scans_barcode_unique ON scans(barcode)")
    except Exception:
        pass


def ensure_db():
    init_wms_db()


def get_session_employee():
    employee_id = str(session.get("employee_id", "")).strip()
    if not employee_id:
        return None
    row = query_one(
        "SELECT id, name, role, badge_number, badge_code, rate, target FROM employees WHERE id = ? AND active = 1",
        (employee_id,),
    )
    if not row:
        session.clear()
        return None
    return {
        "id": row[0],
        "name": row[1],
        "role": row[2] or "staff",
        "badge_number": row[3],
        "badge_code": row[4],
        "rate": float(row[5]),
        "target": int(row[6]),
    }


def is_manager():
    emp = get_session_employee()
    return bool(emp and emp["role"] == "manager")


def log_event(employee_id, operation_type, event_type, shift_id=None, object_code=None, quantity=None, result=None):
    if not employee_id:
        return
    try:
        execute(
            """
            INSERT INTO operation_events
            (id, employee_id, shift_id, operation_type, event_type, object_code, quantity, result, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                employee_id,
                shift_id,
                operation_type,
                event_type,
                object_code,
                quantity,
                result,
                now_iso(),
            ),
        )
    except Exception:
        pass



def ensure_active_shift(employee_id, initial_activity="other"):
    existing = query_one(
        "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (employee_id,),
    )
    if existing:
        return existing[0], False

    shift_id = str(uuid.uuid4())
    ts = now_iso()
    execute(
        "INSERT INTO shifts (id, employee_id, started_at, ended_at) VALUES (?, ?, ?, NULL)",
        (shift_id, employee_id, ts),
    )
    execute(
        "INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at, ended_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (str(uuid.uuid4()), shift_id, employee_id, initial_activity, ts),
    )
    log_event(employee_id, "shift", "start", shift_id=shift_id, result=initial_activity)
    return shift_id, True


def switch_activity(shift_id, employee_id, activity_type):
    current = current_activity(shift_id)
    if current and current["type"] == activity_type:
        return False
    ts = now_iso()
    execute("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL", (ts, shift_id))
    execute(
        "INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at, ended_at) VALUES (?, ?, ?, ?, ?, NULL)",
        (str(uuid.uuid4()), shift_id, employee_id, activity_type, ts),
    )
    log_event(employee_id, activity_type, "switch", shift_id=shift_id, result="active")
    return True


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


LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · Вход</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh}
.wrap{max-width:520px;margin:auto;padding:max(16px,env(safe-area-inset-top)) 16px 20px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:18px;padding:18px;box-shadow:0 2px 10px rgba(52,72,91,.07)}
h1{margin:0 0 7px;font-size:25px}.muted{color:#6b7785;font-size:14px;line-height:1.4}
button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:11px;background:#48647a;color:white;font-size:16px;font-weight:750}
.err,.ok{margin-top:10px;padding:10px;border-radius:10px}.err{background:#f6e4e4;color:#8a4343}.ok{background:#e1f1e6;color:#2f6b45}
#reader{margin-top:12px;overflow:hidden;border-radius:12px}.scanbox{margin-top:14px;padding:14px;border:1px dashed #9fb0bd;border-radius:12px;background:#eef3f6;text-align:center;font-weight:700}
</style></head>
<body><div class="wrap"><div class="card">
<h1>Mini WMS · Вход</h1>
<div class="muted">Вход только по личному QR-бейджу сотрудника. Выбор имени и ручной вход отключены.</div>
<div class="scanbox">Поднеси бейдж к сканеру<br><span class="muted">или используй камеру TSD</span></div>
<button onclick="toggleCamera()">📷 Камера TSD</button>
<div id="reader"></div>
<div id="result"></div>
</div></div>
<script>
let scanner=null,busy=false,hidBuffer='',hidTimer=null;
async function login(code){
 code=(code||'').trim(); if(!code||busy)return; busy=true;
 try{
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({badge_code:code})});
  const d=await r.json();
  if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Бейдж не принят')+'</div>';return}
  localStorage.setItem('fbs_shift_id',d.shift_id||'');
  document.getElementById('result').innerHTML='<div class="ok">Вход: '+String(d.employee.name)+'</div>';
  setTimeout(()=>location.href='/fbs',250);
 }finally{busy=false}
}
document.addEventListener('keydown',e=>{
 if(e.key==='Enter'){
  const code=hidBuffer.trim(); hidBuffer=''; clearTimeout(hidTimer);
  if(code)login(code); return;
 }
 if(e.key.length===1){
  hidBuffer+=e.key;
  clearTimeout(hidTimer);
  hidTimer=setTimeout(()=>{hidBuffer=''},1200);
 }
});
async function toggleCamera(){
 if(scanner){try{await scanner.stop()}catch(e){}scanner=null;document.getElementById('reader').innerHTML='';return}
 if(typeof Html5Qrcode==='undefined')return;
 scanner=new Html5Qrcode('reader');
 try{await scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:280,height:180}},txt=>login(txt),()=>{})}
 catch(e){scanner=null;document.getElementById('result').innerHTML='<div class="err">Камера не открылась</div>'}
}
</script></body></html>"""


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
<a class="manager-only hidden" href="/stats">📊 Статистика</a>
</div>
<div class="card">
<h1>🚀 FBS · Выработка</h1>
<div class="muted">Скан заказа фиксирует сотрудника, время, выработку и оплату. Дубли не засчитываются.</div>
<div class="small" style="margin-top:6px"><span id="identity"></span> · <a href="/exit">🚪 Завершить смену</a></div>
</div>

<div class="card" id="startCard">
<h2 id="startName">Сотрудник</h2>
<div class="muted">Бейдж уже определил сотрудника. Начни или продолжи его смену.</div>
<button onclick="startShift()">Начать / продолжить смену</button>
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
<button class="secondary" onclick="location.href='/exit'">🚪 Завершить смену</button>
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

async function loadIdentity(){
 const r=await fetch('/api/auth/me'),d=await r.json();
 if(!r.ok){location.href='/login';return}
 document.getElementById('identity').textContent=d.employee.name+(d.employee.badge_number?' · бейдж №'+d.employee.badge_number:'');
 document.getElementById('startName').textContent=d.employee.name;
 if(d.employee.role==='manager')document.querySelectorAll('.manager-only').forEach(x=>x.classList.remove('hidden'));
 if(shiftId)await refresh();
}
async function startShift(){
 const r=await fetch('/api/fbs/start',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
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
async function toggleCamera(){
 if(scanner){try{await scanner.stop()}catch(e){}scanner=null;document.getElementById('reader').classList.add('hidden');return}
 if(typeof Html5Qrcode==='undefined'){showErr('Модуль камеры не загрузился');return}
 document.getElementById('reader').classList.remove('hidden');scanner=new Html5Qrcode('reader');
 try{
  await scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:320,height:160}},txt=>sendScan(txt),()=>{});
 }catch(e){scanner=null;showErr('Камера не открылась: '+e)}
}
function showErr(m){beep(false);document.getElementById('result').innerHTML='<div class="err">'+esc(m)+'</div>'}
loadIdentity();setInterval(()=>{if(shiftId)refresh()},15000);
</script></body></html>"""


EXIT_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · Завершение смены</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh}
.wrap{max-width:540px;margin:auto;padding:max(16px,env(safe-area-inset-top)) 16px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:18px;padding:18px;box-shadow:0 2px 10px rgba(52,72,91,.07)}
h1{margin:0 0 8px;font-size:24px}.muted{color:#6b7785;font-size:14px;line-height:1.4}.warning{margin-top:12px;padding:12px;background:#fff4dd;border-radius:11px;color:#70531f}
button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:11px;background:#48647a;color:#fff;font-size:16px;font-weight:750}.secondary{display:block;margin-top:10px;text-align:center;color:#536f83;text-decoration:none}
.err,.ok{margin-top:10px;padding:10px;border-radius:10px}.err{background:#f6e4e4;color:#8a4343}.ok{background:#e1f1e6;color:#2f6b45}
#reader{margin-top:12px;overflow:hidden;border-radius:12px}.scanbox{margin-top:14px;padding:14px;border:1px dashed #9fb0bd;border-radius:12px;background:#eef3f6;text-align:center;font-weight:700}
</style></head><body><div class="wrap"><div class="card">
<h1>🚪 Завершение смены</h1>
<div class="muted">Самостоятельный выход заблокирован. Для завершения смены отсканируй QR старшего в точке сдачи TSD/сканера.</div>
<div class="warning">После подтверждения текущая операция остановится, смена закроется, а TSD выйдет из учётной записи.</div>
<div class="scanbox">Сканируй QR старшего</div>
<button onclick="toggleCamera()">📷 Камера TSD</button>
<div id="reader"></div><div id="result"></div>
<a class="secondary" href="/fbs">← Вернуться без выхода</a>
</div></div>
<script>
let scanner=null,busy=false,hidBuffer='',hidTimer=null;
async function finish(code){
 code=(code||'').trim(); if(!code||busy)return; busy=true;
 try{
  const r=await fetch('/api/auth/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({supervisor_code:code})});
  const d=await r.json();
  if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'QR старшего не принят')+'</div>';return}
  localStorage.removeItem('fbs_shift_id');
  document.getElementById('result').innerHTML='<div class="ok">Смена завершена. TSD свободен.</div>';
  setTimeout(()=>location.href='/login',500);
 }finally{busy=false}
}
document.addEventListener('keydown',e=>{
 if(e.key==='Enter'){const code=hidBuffer.trim();hidBuffer='';clearTimeout(hidTimer);if(code)finish(code);return}
 if(e.key.length===1){hidBuffer+=e.key;clearTimeout(hidTimer);hidTimer=setTimeout(()=>{hidBuffer=''},1200)}
});
async function toggleCamera(){
 if(scanner){try{await scanner.stop()}catch(e){}scanner=null;document.getElementById('reader').innerHTML='';return}
 if(typeof Html5Qrcode==='undefined')return;
 scanner=new Html5Qrcode('reader');
 try{await scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:280,height:180}},txt=>finish(txt),()=>{})}
 catch(e){scanner=null;document.getElementById('result').innerHTML='<div class="err">Камера не открылась</div>'}
}
</script></body></html>"""


SUPERVISOR_QR_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · QR старшего</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:20px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.muted{color:#6b7785;font-size:13px}.top{display:flex;gap:8px;margin-bottom:12px}.top a,.top button{flex:1;padding:10px;border:0;border-radius:10px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}
@media print{.top{display:none}body{background:#fff}.wrap{padding:0}.card{border:0}}
</style></head><body><div class="wrap">
<div class="top"><a href="/stats">← Статистика</a><button onclick="print()">Печать</button></div>
<div class="card"><h1>QR завершения смены</h1><div class="muted">QR автоматически меняется каждые 30 секунд. Старый код быстро перестаёт работать.</div><div id="qr" class="qr"></div><b>СТАРШИЙ · ВЫХОД</b><div class="muted" style="margin-top:8px">Обновление через <span id="ttl">—</span> сек.</div></div>
</div><script>
let lastCode='';
async function refreshQr(){
 try{
  const r=await fetch('/api/admin/supervisor-exit-code',{cache:'no-store'});
  if(!r.ok)return;
  const d=await r.json();
  document.getElementById('ttl').textContent=d.expires_in;
  if(d.code!==lastCode){
   lastCode=d.code;
   const box=document.getElementById('qr');
   box.innerHTML='';
   new QRCode(box,{text:d.code,width:260,height:260});
  }
 }catch(e){}
}
refreshQr();setInterval(refreshQr,1000);
</script></body></html>"""


SUPERVISOR_TERMINAL_SETUP_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · Терминал выхода</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:560px;margin:auto;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:20px}.muted{color:#6b7785;font-size:14px;line-height:1.45}.note{margin-top:14px;padding:12px;border-radius:11px;background:#eef3f6}.btn{width:100%;margin-top:14px;padding:14px;border:0;border-radius:11px;background:#48647a;color:#fff;font-size:16px;font-weight:750;cursor:pointer}.back{display:block;text-align:center;margin-top:12px;color:#536f83;text-decoration:none}.err{margin-top:12px;padding:10px;border-radius:10px;background:#f6e4e4;color:#8a4343}
</style></head><body><div class="wrap"><div class="card">
<h1>📟 Терминал выхода</h1>
<div class="muted">Авторизуй это устройство один раз. После этого твоя учётка Виктора будет удалена из этой браузерной сессии, а устройство сможет показывать только динамический QR выхода.</div>
<div class="note"><b>Важно:</b> это не завершает твою рабочую смену в WMS. Оно только убирает твою авторизацию с этого конкретного устройства.</div>
<button class="btn" onclick="activate()">Авторизовать это устройство</button>
<div id="result"></div>
<a class="back" href="/stats">← Назад в статистику</a>
</div></div>
<script>
async function activate(){
 const r=await fetch('/api/supervisor-terminal/activate',{method:'POST'});
 const d=await r.json();
 if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Ошибка')+'</div>';return}
 localStorage.setItem('supervisor_terminal_token',d.terminal_token);
 localStorage.removeItem('fbs_shift_id');
 location.href='/supervisor-terminal';
}
</script></body></html>"""


SUPERVISOR_TERMINAL_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · QR выхода</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh;display:flex;align-items:center;justify-content:center}.wrap{width:100%;max-width:560px;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:20px;padding:22px;text-align:center;box-shadow:0 2px 12px rgba(52,72,91,.08)}.muted{color:#6b7785;font-size:14px;line-height:1.4}.qr{display:flex;justify-content:center;margin:20px 0}.ttl{font-size:34px;font-weight:850}.err{margin-top:12px;padding:12px;border-radius:10px;background:#f6e4e4;color:#8a4343}.link{display:inline-block;margin-top:12px;color:#536f83;text-decoration:none}
</style></head><body><div class="wrap"><div class="card">
<h1>🔐 QR старшего</h1>
<div class="muted">Терминал авторизован отдельно от учётки руководителя. QR меняется каждые 30 секунд.</div>
<div id="qr" class="qr"></div>
<div class="ttl"><span id="ttl">—</span> сек.</div>
<div class="muted">до обновления QR</div>
<div id="result"></div>
</div></div>
<script>
let lastCode='';
async function refreshQr(){
 const token=localStorage.getItem('supervisor_terminal_token')||'';
 if(!token){document.getElementById('result').innerHTML='<div class="err">Это устройство не авторизовано как терминал старшего.</div>';return}
 try{
  const r=await fetch('/api/supervisor-terminal/qr',{cache:'no-store',headers:{'X-Supervisor-Terminal':token}});
  const d=await r.json();
  if(!r.ok){
   localStorage.removeItem('supervisor_terminal_token');
   document.getElementById('qr').innerHTML='';
   document.getElementById('ttl').textContent='—';
   document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Авторизация терминала недействительна')+'</div>';
   return;
  }
  document.getElementById('result').innerHTML='';
  document.getElementById('ttl').textContent=d.expires_in;
  if(d.code!==lastCode){
   lastCode=d.code;
   const box=document.getElementById('qr');box.innerHTML='';
   new QRCode(box,{text:d.code,width:280,height:280});
  }
 }catch(e){}
}
refreshQr();setInterval(refreshQr,1000);
</script></body></html>"""


STATS_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · Статистика</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.wrap{max-width:980px;margin:auto;padding:14px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;margin-bottom:10px;box-shadow:0 2px 10px rgba(52,72,91,.07)}
.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-bottom:10px}.nav a{display:flex;align-items:center;justify-content:center;min-height:40px;border-radius:10px;text-decoration:none;font-weight:750;background:#dde8ef;color:#314657}.nav a.active{background:#48647a;color:#fff}
h1{margin:0 0 6px}.big{font-size:28px;font-weight:850}.small{font-size:12px;color:#6b7785}.worker{display:grid;grid-template-columns:1.2fr repeat(4,1fr);gap:8px;padding:11px 0;border-bottom:1px solid #dde5eb}.worker:last-child{border:0}
.actions{display:flex;gap:8px;flex-wrap:wrap}.actions a{padding:9px 12px;border-radius:9px;background:#dde8ef;color:#314657;text-decoration:none;font-weight:700}
@media(max-width:700px){.worker{grid-template-columns:1fr 1fr}.name{grid-column:1/-1}}
</style></head><body><div class="wrap">
<div class="nav"><a href="/">📦 Приёмка</a><a href="/fbs">🚀 FBS</a><a class="active" href="/stats">📊 Статистика</a></div>
<div class="card"><h1>📊 Статистика</h1><div class="small">Доступ руководителя. Сотрудникам этот раздел не показывается.</div><div class="actions" style="margin-top:10px"><a href="/badges">🪪 Бейджи сотрудников</a><a href="/supervisor-qr">🔐 QR завершения смены</a><a href="/supervisor-terminal/setup">📟 Настроить терминал выхода</a><a href="/exit">🚪 Завершить мою смену</a></div></div>
<div class="card"><div class="small">Активные смены</div><div class="big" id="active">0</div></div>
<div class="card"><b>Сейчас работают</b><div id="workers" style="margin-top:8px">Загрузка…</div></div>
<div class="card"><b>Сотрудники</b><div id="employees" style="margin-top:8px">Загрузка…</div></div>
</div>
<script>
function esc(s){return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
async function load(){
 const r=await fetch('/api/fbs/dashboard'); if(r.status===403){location.href='/fbs';return} const d=await r.json();
 document.getElementById('active').textContent=d.workers.length;
 const box=document.getElementById('workers');
 box.innerHTML=d.workers.length?d.workers.map(x=>'<div class="worker"><div class="name"><b>'+esc(x.employee)+'</b><div class="small">'+esc(x.activity||'')+'</div></div><div><b>'+x.orders+'</b><div class="small">заказов</div></div><div><b>'+x.orders_per_hour+'</b><div class="small">заказ/ч</div></div><div><b>'+x.earned.toLocaleString('ru-RU')+' ₽</b><div class="small">заработано</div></div><div><b>'+x.percent+'%</b><div class="small">от '+x.target+'</div></div></div>').join(''):'Нет активных смен';
 const er=await fetch('/api/stats/employees'),ed=await er.json();
 document.getElementById('employees').innerHTML=ed.employees.map(x=>'<div class="worker"><div class="name"><b>'+esc(x.name)+'</b><div class="small">бейдж №'+(x.badge_number||'—')+' · '+esc(x.role)+'</div></div><div><b>'+x.shifts+'</b><div class="small">смен</div></div><div><b>'+x.orders+'</b><div class="small">FBS заказов</div></div><div><b>'+x.picking_hours+'</b><div class="small">ч чистой сборки</div></div><div><b>'+x.avg_speed+'</b><div class="small">ср. заказ/ч</div></div></div>').join('');
}
load();setInterval(load,10000);
</script></body></html>"""


BADGES_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>Mini WMS · Бейджи</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:900px;margin:auto;padding:16px}
.top{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}.top a,.top button{padding:10px 12px;border:0;border-radius:10px;background:#48647a;color:white;text-decoration:none;font-weight:750}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}.badge{background:#fff;border:1px solid #d7e0e7;border-radius:16px;padding:16px;text-align:center}.qr{display:flex;justify-content:center;margin:12px 0}.num{font-size:34px;font-weight:850}.small{font-size:12px;color:#6b7785}
@media print{body{background:#fff}.top{display:none}.badge{break-inside:avoid;box-shadow:none}}
</style></head><body><div class="wrap"><div class="top"><a href="/stats">← Статистика</a><button onclick="print()">Печать бейджей</button></div><div id="grid" class="grid"></div></div>
<script>
async function load(){
 const r=await fetch('/api/admin/badges');if(r.status===403){location.href='/fbs';return}const d=await r.json();
 const g=document.getElementById('grid');
 g.innerHTML=d.employees.map(x=>'<div class="badge"><div class="small">MINI WMS</div><div class="num">№'+x.badge_number+'</div><b>'+x.name+'</b><div class="small">'+(x.role==='manager'?'Руководитель':'Сотрудник')+'</div><div class="qr" id="q_'+x.id+'"></div><div class="small">Личный ID · не передавать другому сотруднику</div></div>').join('');
 d.employees.forEach(x=>new QRCode(document.getElementById('q_'+x.id),{text:x.badge_code,width:150,height:150}));
}
load();
</script></body></html>"""


@wms_bp.get("/setup/viktor-8f4c2e91a7b6")
def bootstrap_viktor_badge():
    ensure_db()
    code = make_badge_code("viktor")
    page = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Mini WMS · Бейдж Виктора</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:18px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:22px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.num{font-size:34px;font-weight:850}.small{font-size:12px;color:#6b7785}.btn{display:block;margin-top:14px;padding:12px;border-radius:11px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}@media print{body{background:#fff}.wrap{padding:0}.btn,.hint{display:none}.card{border:0}}
</style></head><body><div class="wrap"><div class="card">
<div class="small">MINI WMS</div><div class="num">№1</div><h2>Виктор</h2><div class="small">Руководитель склада · Manager</div>
<div id="qr" class="qr"></div><div class="small">Личный бейдж входа</div>
<a class="btn" href="/login">Перейти ко входу</a><div class="small hint" style="margin-top:12px">Сохрани или распечатай этот QR. Он предназначен для входа Виктора в Mini WMS.</div>
</div></div><script>new QRCode(document.getElementById('qr'),{text:"__VIKTOR_BADGE_CODE__",width:260,height:260});</script></body></html>"""
    return page


@wms_bp.get("/login")
def login_page():
    ensure_db()
    if get_session_employee():
        return redirect("/fbs")
    return LOGIN_PAGE


@wms_bp.post("/api/auth/login")
def auth_login():
    ensure_db()
    data = request.get_json(force=True)
    badge_code = str(data.get("badge_code", "")).strip()
    if not badge_code:
        return jsonify(error="Пустой код бейджа"), 400
    row = query_one(
        "SELECT id, name, role, badge_number FROM employees WHERE badge_code = ? AND active = 1",
        (badge_code,),
    )
    if not row:
        return jsonify(error="Бейдж не найден"), 404
    session.clear()
    session["employee_id"] = row[0]
    shift_id, _ = ensure_active_shift(row[0], "other")
    log_event(row[0], "auth", "login", shift_id=shift_id, object_code=badge_code, result="ok")
    return jsonify(ok=True, shift_id=shift_id, employee={"id": row[0], "name": row[1], "role": row[2], "badge_number": row[3]})


@wms_bp.get("/exit")
def exit_page():
    ensure_db()
    if not get_session_employee():
        return redirect("/login")
    return EXIT_PAGE


@wms_bp.get("/supervisor-terminal/setup")
def supervisor_terminal_setup_page():
    ensure_db()
    if not is_manager():
        return redirect("/fbs")
    return SUPERVISOR_TERMINAL_SETUP_PAGE


@wms_bp.post("/api/supervisor-terminal/activate")
def supervisor_terminal_activate():
    ensure_db()
    emp = get_session_employee()
    if not emp or emp["role"] != "manager":
        return jsonify(error="Доступ только руководителю"), 403
    token = issue_supervisor_terminal_token()
    log_event(emp["id"], "supervisor_terminal", "activate", result="ok")
    session.clear()
    return jsonify(ok=True, terminal_token=token)


@wms_bp.get("/supervisor-terminal")
def supervisor_terminal_page():
    return SUPERVISOR_TERMINAL_PAGE


@wms_bp.get("/api/supervisor-terminal/qr")
def supervisor_terminal_qr():
    token = request.headers.get("X-Supervisor-Terminal", "")
    if not valid_supervisor_terminal_token(token):
        return jsonify(error="Терминал не авторизован"), 401
    code, step, remaining = current_supervisor_exit_code()
    return jsonify(code=code, expires_in=remaining, period=SUPERVISOR_QR_PERIOD, step=step)


@wms_bp.get("/supervisor-qr")
def supervisor_qr_page():
    ensure_db()
    if not is_manager():
        return redirect("/fbs")
    return SUPERVISOR_QR_PAGE


@wms_bp.get("/api/admin/supervisor-exit-code")
def supervisor_exit_code_api():
    ensure_db()
    if not is_manager():
        return jsonify(error="Доступ только руководителю"), 403
    code, step, remaining = current_supervisor_exit_code()
    return jsonify(code=code, expires_in=remaining, period=SUPERVISOR_QR_PERIOD, step=step)


@wms_bp.post("/api/auth/logout")
def auth_logout():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Активная учётная запись не найдена"), 401

    data = request.get_json(silent=True) or {}
    supervisor_code = str(data.get("supervisor_code", "")).strip()
    if not valid_supervisor_exit_code(supervisor_code):
        log_event(emp["id"], "auth", "logout_denied", object_code="invalid-supervisor-qr", result="denied")
        return jsonify(error="Нужен QR старшего. Смена не завершена."), 403

    row = query_one(
        "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (emp["id"],),
    )
    if row:
        shift_id = row[0]
        ts = now_iso()
        execute("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL", (ts, shift_id))
        execute("UPDATE shifts SET ended_at = ? WHERE id = ?", (ts, shift_id))
        log_event(emp["id"], "shift", "end", shift_id=shift_id, object_code="supervisor-qr", result="ok")

    log_event(emp["id"], "auth", "logout", object_code="supervisor-qr", result="ok")
    session.clear()
    return jsonify(ok=True)


@wms_bp.get("/api/auth/me")
def auth_me():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Требуется вход"), 401
    return jsonify(employee=emp)


@wms_bp.post("/api/operation/enter")
def operation_enter():
    ensure_db()
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    data = request.get_json(silent=True) or {}
    operation = str(data.get("operation", "")).strip()
    allowed = {"picking", "replenishment", "receiving", "shipping", "pallets", "kgt", "downtime", "other"}
    if operation not in allowed:
        return jsonify(error="Неизвестная операция"), 400
    shift_id, _ = ensure_active_shift(employee["id"], operation)
    switch_activity(shift_id, employee["id"], operation)
    return jsonify(ok=True, shift_id=shift_id, operation=operation)


@wms_bp.get("/api/shift/current")
def current_shift_api():
    ensure_db()
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    row = query_one(
        "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (employee["id"],),
    )
    if not row:
        return jsonify(active=False)
    return jsonify(active=True, stats=shift_stats(row[0]))


@wms_bp.get("/stats")
def stats_page():
    ensure_db()
    if not is_manager():
        return redirect("/fbs")
    return STATS_PAGE


@wms_bp.get("/fbs-manager")
def old_manager_page():
    return redirect("/stats")


@wms_bp.get("/badges")
def badges_page():
    ensure_db()
    if not is_manager():
        return redirect("/fbs")
    return BADGES_PAGE


@wms_bp.get("/api/admin/badges")
def admin_badges():
    ensure_db()
    if not is_manager():
        return jsonify(error="Доступ только руководителю"), 403
    rows = query_all("SELECT id, name, role, badge_number, badge_code FROM employees WHERE active = 1 ORDER BY badge_number, name")
    return jsonify(employees=[
        {"id": r[0], "name": r[1], "role": r[2], "badge_number": r[3], "badge_code": r[4]}
        for r in rows
    ])


@wms_bp.get("/api/stats/employees")
def stats_employees():
    ensure_db()
    if not is_manager():
        return jsonify(error="Доступ только руководителю"), 403
    rows = query_all("SELECT id, name, role, badge_number FROM employees WHERE active = 1 ORDER BY badge_number, name")
    out = []
    for emp_id, name, role, badge_number in rows:
        shifts_row = query_one("SELECT COUNT(*) FROM shifts WHERE employee_id = ?", (emp_id,))
        orders_row = query_one("SELECT COUNT(*) FROM scans WHERE employee_id = ?", (emp_id,))
        secs = 0.0
        shift_ids = query_all("SELECT id FROM shifts WHERE employee_id = ?", (emp_id,))
        for sr in shift_ids:
            secs += picking_seconds(sr[0])
        orders = int(orders_row[0] if orders_row else 0)
        hours = secs / 3600.0
        out.append({
            "id": emp_id,
            "name": name,
            "role": role,
            "badge_number": badge_number,
            "shifts": int(shifts_row[0] if shifts_row else 0),
            "orders": orders,
            "picking_hours": round(hours, 1),
            "avg_speed": round(orders / hours, 1) if hours > 0 else 0.0,
        })
    return jsonify(employees=out)


@wms_bp.get("/fbs")
def fbs_page():
    ensure_db()
    if not get_session_employee():
        return redirect("/login")
    return FBS_PAGE


@wms_bp.get("/api/fbs/employees")
def employees():
    ensure_db()
    if not is_manager():
        return jsonify(error="Доступ только руководителю"), 403
    rows = query_all("SELECT id, name, rate, target FROM employees WHERE active = 1 ORDER BY name")
    return jsonify(employees=[
        {"id": r[0], "name": r[1], "rate": float(r[2]), "target": int(r[3])}
        for r in rows
    ])


@wms_bp.post("/api/fbs/start")
def start_shift():
    ensure_db()
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    employee_id = employee["id"]
    emp = (employee_id, employee["name"])

    existing = query_one(
        "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (employee_id,),
    )
    if existing:
        switch_activity(existing[0], employee_id, "picking")
        return jsonify(ok=True, shift_id=existing[0], resumed=True)

    shift_id, _ = ensure_active_shift(employee_id, "picking")
    return jsonify(ok=True, shift_id=shift_id, resumed=False)


@wms_bp.post("/api/fbs/scan")
def fbs_scan():
    ensure_db()
    data = request.get_json(force=True)
    shift_id = str(data.get("shift_id", "")).strip()
    barcode = str(data.get("barcode", "")).strip()
    if not barcode:
        return jsonify(error="Пустой штрихкод"), 400

    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401

    shift = query_one(
        "SELECT employee_id, ended_at FROM shifts WHERE id = ?",
        (shift_id,),
    )
    if not shift:
        return jsonify(error="Смена не найдена"), 404
    if shift[1]:
        return jsonify(error="Смена уже завершена"), 409
    if shift[0] != employee["id"]:
        return jsonify(error="Эта смена принадлежит другому сотруднику"), 403

    active = current_activity(shift_id)
    if not active or active["type"] != "picking":
        return jsonify(error="Сейчас включена не сборка. Переключи операцию на «Сборка»."), 409

    duplicate = query_one(
        "SELECT s.id, e.name FROM scans s JOIN employees e ON e.id = s.employee_id WHERE s.barcode = ? LIMIT 1",
        (barcode,),
    )
    if duplicate:
        return jsonify(error="Этот заказ уже был засчитан: " + str(duplicate[1])), 409

    try:
        execute(
            "INSERT INTO scans (id, shift_id, employee_id, barcode, scanned_at, marketplace) VALUES (?, ?, ?, ?, ?, 'WB')",
            (str(uuid.uuid4()), shift_id, shift[0], barcode, now_iso()),
        )
    except Exception:
        duplicate = query_one(
            "SELECT e.name FROM scans s JOIN employees e ON e.id = s.employee_id WHERE s.barcode = ? LIMIT 1",
            (barcode,),
        )
        if duplicate:
            return jsonify(error="Этот заказ уже был засчитан: " + str(duplicate[0])), 409
        raise

    log_event(shift[0], "fbs", "scan", shift_id=shift_id, object_code=barcode, quantity=1, result="ok")
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

    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    shift = query_one("SELECT employee_id, ended_at FROM shifts WHERE id = ?", (shift_id,))
    if not shift or shift[1]:
        return jsonify(error="Активная смена не найдена"), 404
    if shift[0] != employee["id"]:
        return jsonify(error="Эта смена принадлежит другому сотруднику"), 403

    changed = switch_activity(shift_id, shift[0], activity_type)
    return jsonify(ok=True, unchanged=not changed)


@wms_bp.post("/api/fbs/end")
def end_shift():
    ensure_db()
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    data = request.get_json(silent=True) or {}
    supervisor_code = str(data.get("supervisor_code", "")).strip()
    if not valid_supervisor_exit_code(supervisor_code):
        return jsonify(error="Завершение смены возможно только по QR старшего"), 403
    shift_id = str(data.get("shift_id", "")).strip()
    shift = query_one("SELECT id, employee_id, ended_at FROM shifts WHERE id = ?", (shift_id,))
    if not shift:
        return jsonify(error="Смена не найдена"), 404
    if shift[1] != employee["id"] and not is_manager():
        return jsonify(error="Эта смена принадлежит другому сотруднику"), 403
    if shift[2]:
        return jsonify(ok=True, already_ended=True)
    ts = now_iso()
    execute("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL", (ts, shift_id))
    execute("UPDATE shifts SET ended_at = ? WHERE id = ?", (ts, shift_id))
    log_event(shift[1], "shift", "end", shift_id=shift_id, object_code="supervisor-qr", result="ok")
    return jsonify(ok=True, stats=shift_stats(shift_id))


@wms_bp.get("/api/fbs/stats")
def stats():
    ensure_db()
    shift_id = str(request.args.get("shift_id", "")).strip()
    result = shift_stats(shift_id)
    if not result:
        return jsonify(error="Смена не найдена"), 404
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    if result["employee_id"] != employee["id"] and not is_manager():
        return jsonify(error="Нет доступа к этой смене"), 403
    return jsonify(**result)


@wms_bp.get("/api/fbs/dashboard")
def dashboard():
    ensure_db()
    if not is_manager():
        return jsonify(error="Доступ только руководителю"), 403
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
