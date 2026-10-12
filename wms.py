import io
import os
import sqlite3
import uuid
import hashlib
import hmac
import json
import base64
import time
import threading
from datetime import datetime, timezone
from contextlib import contextmanager

from flask import Blueprint, jsonify, request, session, redirect, Response

wms_bp = Blueprint("wms", __name__)

RATE = float(os.getenv("FBS_RATE", "2.25"))
TARGET = int(os.getenv("FBS_TARGET", "1600"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_PATH = os.getenv("WMS_DB_PATH", "/tmp/wms.db")
REQUIRE_POSTGRES = os.getenv("WMS_REQUIRE_POSTGRES", "0").strip().lower() in ("1", "true", "yes", "on")
_WMS_BADGE_SECRET = os.getenv("WMS_BADGE_SECRET", "").strip()
_WMS_TERMINAL_SECRET = os.getenv("WMS_TERMINAL_SECRET", "").strip()
if not _WMS_BADGE_SECRET:
    raise RuntimeError("WMS_BADGE_SECRET is required in the environment")
if not _WMS_TERMINAL_SECRET:
    raise RuntimeError("WMS_TERMINAL_SECRET is required in the environment")

WMS_BADGE_SECRET = _WMS_BADGE_SECRET.encode("utf-8")
SUPERVISOR_QR_PERIOD = max(10, int(os.getenv("SUPERVISOR_QR_PERIOD", "30")))
WMS_TERMINAL_SECRET = _WMS_TERMINAL_SECRET.encode("utf-8")
SUPERVISOR_TERMINAL_VERSION = os.getenv("SUPERVISOR_TERMINAL_VERSION", "1").strip() or "1"
MANAGER_WEB_PASSWORD = os.getenv("MANAGER_WEB_PASSWORD", "").strip()
MANAGER_LOGIN_MAX_ATTEMPTS = max(3, int(os.getenv("MANAGER_LOGIN_MAX_ATTEMPTS", "8") or 8))
MANAGER_LOGIN_WINDOW_SECONDS = max(60, int(os.getenv("MANAGER_LOGIN_WINDOW_SECONDS", "600") or 600))
MANAGER_LOGIN_BLOCK_SECONDS = max(60, int(os.getenv("MANAGER_LOGIN_BLOCK_SECONDS", "900") or 900))
_MANAGER_LOGIN_LOCK = threading.Lock()
_MANAGER_LOGIN_ATTEMPTS = {}
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "").strip()
WB_STRICT_MATCHING = os.getenv("WB_STRICT_MATCHING", "0").strip().lower() in ("1", "true", "yes", "on")


def make_token(purpose):
    digest = hmac.new(WMS_BADGE_SECRET, purpose.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:32]


def make_badge_code(employee_id):
    return "WMS-BADGE-" + make_token("employee:" + employee_id)


def make_badge_scanner_code(employee_id):
    # Только цифры: HID-сканер не зависит от RU/EN раскладки клавиатуры.
    digest = hmac.new(
        WMS_BADGE_SECRET,
        ("employee-scanner:" + employee_id).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "91" + f"{int(digest, 16) % (10 ** 18):018d}"


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
_DB_READY = False
_DB_INIT_LOCK = threading.Lock()


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

    conn = sqlite3.connect(DB_PATH, timeout=3)
    try:
        conn.execute("PRAGMA busy_timeout=3000")
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
    global _DB_READY
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
            badge_code TEXT,
            scanner_code TEXT
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
            product_barcode TEXT,
            article TEXT,
            product_name TEXT,
            wb_order_id TEXT,
            scan_type TEXT NOT NULL DEFAULT 'normal',
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
            article TEXT,
            product_name TEXT,
            document_name TEXT,
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
        """
        CREATE TABLE IF NOT EXISTS wb_orders (
            order_id TEXT PRIMARY KEY,
            wb_barcode TEXT UNIQUE,
            article TEXT,
            skus_json TEXT NOT NULL DEFAULT '[]',
            nm_id TEXT,
            chrt_id TEXT,
            supply_id TEXT,
            synced_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS daily_targets (
            date_key TEXT PRIMARY KEY,
            wb_fbs_plan INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            updated_by TEXT
        )
        """,
    ]
    with db_connect() as conn:
        cur = conn.cursor()
        if not DATABASE_URL:
            # WAL lets dashboard reads continue while scanners write events.
            cur.execute("PRAGMA journal_mode=WAL")
            cur.fetchone()
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=3000")
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
            ("scanner_code", "TEXT"),
        ]
        for name, ddl in additions:
            if name not in cols:
                cur.execute(f"ALTER TABLE employees ADD COLUMN {name} {ddl}")

        # Миграция FBS-журнала: связываем WB-заказ с реально отсканированным товаром.
        if DATABASE_URL:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'scans'")
            scan_cols = {r[0] for r in cur.fetchall()}
        else:
            cur.execute("PRAGMA table_info(scans)")
            scan_cols = {r[1] for r in cur.fetchall()}

        scan_additions = [
            ("product_barcode", "TEXT"),
            ("article", "TEXT"),
            ("product_name", "TEXT"),
            ("wb_order_id", "TEXT"),
            ("scan_type", "TEXT NOT NULL DEFAULT 'normal'"),
        ]
        for name, ddl in scan_additions:
            if name not in scan_cols:
                cur.execute(f"ALTER TABLE scans ADD COLUMN {name} {ddl}")

        # Метаданные операций нужны для desktop-мониторинга без повторных запросов к МойСклад.
        if DATABASE_URL:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'operation_events'")
            event_cols = {r[0] for r in cur.fetchall()}
        else:
            cur.execute("PRAGMA table_info(operation_events)")
            event_cols = {r[1] for r in cur.fetchall()}

        event_additions = [
            ("article", "TEXT"),
            ("product_name", "TEXT"),
            ("document_name", "TEXT"),
        ]
        for name, ddl in event_additions:
            if name not in event_cols:
                cur.execute(f"ALTER TABLE operation_events ADD COLUMN {name} {ddl}")

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
        scanner_code = make_badge_scanner_code(emp_id)
        if query_one("SELECT id FROM employees WHERE id = ?", (emp_id,)):
            execute(
                "UPDATE employees SET name = ?, role = ?, badge_number = ?, badge_code = ?, scanner_code = ?, rate = ?, target = ?, active = 1 WHERE id = ?",
                (name, role, badge_number, badge_code, scanner_code, RATE, TARGET, emp_id),
            )
        else:
            execute(
                "INSERT INTO employees (id, name, rate, target, active, role, badge_number, badge_code, scanner_code) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)",
                (emp_id, name, RATE, TARGET, role, badge_number, badge_code, scanner_code),
            )

    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_badge_code ON employees(badge_code)")
    except Exception:
        pass
    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_scans_barcode_unique ON scans(barcode)")
    except Exception:
        pass
    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_scanner_code ON employees(scanner_code)")
    except Exception:
        pass
    try:
        execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_wb_orders_barcode ON wb_orders(wb_barcode)")
    except Exception:
        pass

    # Dashboard/read indexes. These keep live monitoring fast as scan/event
    # history grows instead of forcing full-table scans every refresh.
    for index_sql in (
        "CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at)",
        "CREATE INDEX IF NOT EXISTS idx_scans_employee_scanned_at ON scans(employee_id, scanned_at)",
        "CREATE INDEX IF NOT EXISTS idx_shifts_active_started ON shifts(ended_at, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_activities_shift_type_started ON activities(shift_id, activity_type, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_operation_events_op_created ON operation_events(operation_type, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_operation_events_event_created ON operation_events(event_type, created_at)",
    ):
        try:
            execute(index_sql)
        except Exception:
            pass

    _DB_READY = True


def ensure_db():
    global _DB_READY
    if _DB_READY:
        return
    with _DB_INIT_LOCK:
        if _DB_READY:
            return
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


def manager_access():
    # Desktop manager session is already authenticated; avoid an unnecessary
    # SQLite lookup on every dashboard API request.
    return bool(session.get("manager_web") is True or is_manager())


def _manager_client_ip():
    forwarded = str(request.headers.get("X-Forwarded-For", "")).strip()
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return str(request.remote_addr or "unknown")


def _manager_rate_state(ip, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else time.time())
    with _MANAGER_LOGIN_LOCK:
        state = _MANAGER_LOGIN_ATTEMPTS.get(ip)
        if not state:
            return {"blocked": False, "remaining": 0, "attempts": 0}

        blocked_until = float(state.get("blocked_until", 0) or 0)
        if blocked_until > now_ts:
            return {
                "blocked": True,
                "remaining": int(blocked_until - now_ts) + 1,
                "attempts": int(state.get("attempts", 0) or 0),
            }

        window_started = float(state.get("window_started", 0) or 0)
        if now_ts - window_started >= MANAGER_LOGIN_WINDOW_SECONDS:
            _MANAGER_LOGIN_ATTEMPTS.pop(ip, None)
            return {"blocked": False, "remaining": 0, "attempts": 0}

        return {
            "blocked": False,
            "remaining": 0,
            "attempts": int(state.get("attempts", 0) or 0),
        }


def _manager_register_failed_login(ip, now_ts=None):
    now_ts = float(now_ts if now_ts is not None else time.time())
    with _MANAGER_LOGIN_LOCK:
        state = _MANAGER_LOGIN_ATTEMPTS.get(ip)
        if not state or now_ts - float(state.get("window_started", 0) or 0) >= MANAGER_LOGIN_WINDOW_SECONDS:
            state = {"window_started": now_ts, "attempts": 0, "blocked_until": 0}
        state["attempts"] = int(state.get("attempts", 0) or 0) + 1
        if state["attempts"] >= MANAGER_LOGIN_MAX_ATTEMPTS:
            state["blocked_until"] = now_ts + MANAGER_LOGIN_BLOCK_SECONDS
        _MANAGER_LOGIN_ATTEMPTS[ip] = state
        return dict(state)


def _manager_clear_failed_logins(ip):
    with _MANAGER_LOGIN_LOCK:
        _MANAGER_LOGIN_ATTEMPTS.pop(ip, None)


def log_event(
    employee_id,
    operation_type,
    event_type,
    shift_id=None,
    object_code=None,
    quantity=None,
    result=None,
    article=None,
    product_name=None,
    document_name=None,
):
    if not employee_id:
        return
    try:
        execute(
            """
            INSERT INTO operation_events
            (id, employee_id, shift_id, operation_type, event_type, object_code, quantity, result,
             article, product_name, document_name, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                article,
                product_name,
                document_name,
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
        "activity_started_at": active["started_at"] if active else None,
        "target_done": orders >= target,
    }


LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="screen-orientation" content="landscape">
<title>ONIX WMS · Вход</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>
<style>
*{box-sizing:border-box}
html,body{margin:0;width:100%;min-height:100%;background:#e9eef3;color:#25313d}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100dvh;-webkit-text-size-adjust:100%;overflow:hidden}
.wrap{width:100%;height:100dvh;padding:max(7px,env(safe-area-inset-top)) max(9px,env(safe-area-inset-right)) max(7px,env(safe-area-inset-bottom)) max(9px,env(safe-area-inset-left));display:flex;align-items:center;justify-content:center}
.card{width:min(980px,100%);height:100%;background:#f7f9fb;border:1px solid #d7e0e7;border-radius:14px;padding:8px 10px;box-shadow:0 2px 10px rgba(52,72,91,.07);display:grid;grid-template-rows:auto 1fr auto;gap:7px;overflow:hidden}
.header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-bottom:6px;border-bottom:1px solid #d7e0e7;min-height:42px}
.header-left{display:flex;align-items:center;gap:9px;min-width:0}
.logo{width:32px;height:32px;border-radius:9px;background:#48647a;color:#fff;display:grid;place-items:center;font-weight:900;font-size:12px;letter-spacing:.4px;flex:0 0 auto}
.header-copy{min-width:0}
h1{margin:0;font-size:20px;line-height:1}.muted{color:#6b7785;font-size:10px;line-height:1.25}
.status{padding:7px 10px;border-radius:9px;background:#eef3f6;border:1px solid #d7e0e7;font-size:10px;font-weight:800;white-space:nowrap}
.status.ready{background:#e1f1e6;color:#2f6b45;border-color:#c7e3d0}
.main{display:grid;grid-template-columns:1fr minmax(190px,32%);gap:9px;min-height:0}
.left{display:flex;flex-direction:column;justify-content:center;min-width:0;padding:3px 6px}
.subtitle{font-size:13px;color:#53616d;line-height:1.35;max-width:620px}
.scan-input{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
.camera-box{display:flex;flex-direction:column;justify-content:flex-start;align-items:stretch;min-height:0}
.camera-title{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:4px}
.camera-title span{font-size:10px;color:#6b7785}
button{border:0;border-radius:8px;background:#48647a;color:white;font-size:11px;font-weight:850;padding:7px 9px;cursor:pointer}
#reader{border-radius:9px;overflow:hidden;background:#111;min-height:78px;max-height:130px}
#reader:empty{display:flex;align-items:center;justify-content:center;background:#eef3f6;border:1px dashed #b8c5ce}
#reader:empty:before{content:"Камера выключена";font-size:10px;color:#7b8791}
#reader video{width:100%!important;height:100%!important;max-height:130px!important;object-fit:cover!important}
#reader__dashboard_section_csr button,#reader__dashboard_section_swaplink{display:none!important}
#result{margin-top:4px}.err,.ok{padding:5px 7px;border-radius:8px;font-size:9px}.err{background:#f6e4e4;color:#8a4343}.ok{background:#e1f1e6;color:#2f6b45}
.barcode-zone{border-top:1px solid #d7e0e7;padding-top:5px;min-height:92px}
.barcode-wrap{height:88px;background:#fff;border:1px solid #d7e0e7;border-radius:9px;padding:3px 8px;display:flex;align-items:center;justify-content:center;overflow:hidden}
#zebraBarcode{width:100%;height:82px}
@media (orientation:portrait){
 body{overflow:auto}.wrap{height:auto;min-height:100dvh}.card{height:auto;min-height:calc(100dvh - 14px)}.main{grid-template-columns:1fr}.camera-box{min-height:150px}.status{white-space:normal}
}
@media (orientation:landscape) and (max-height:420px){
 .card{padding:5px 7px;gap:4px}.header{min-height:34px;padding-bottom:4px}.logo{width:28px;height:28px;font-size:11px}h1{font-size:17px}.muted{font-size:8px}.status{padding:5px 7px;font-size:9px}
 .main{gap:6px}.left{padding:2px 4px}.subtitle{font-size:11px}.camera-title{margin-bottom:3px}.camera-title span{font-size:9px}button{padding:5px 7px;font-size:9px}
 #reader{min-height:62px;max-height:88px}#reader video{max-height:88px!important}
 .barcode-zone{min-height:74px;padding-top:3px}.barcode-wrap{height:70px;padding:2px 5px}#zebraBarcode{height:66px}
}
</style></head>
<body><div class="wrap"><div class="card">
<div class="header">
  <div class="header-left">
    <div class="logo">ONX</div>
    <div class="header-copy"><h1>ONIX WMS</h1><div class="muted">Терминал сотрудника</div></div>
  </div>
  <div id="scannerStatus" class="status">Zebra не подключена</div>
</div>
<div class="main">
  <div class="left">
    <div class="subtitle"><b>Вход через Zebra</b><br>1. Сканируй нижний код подключения<br>2. Сканируй бейдж сотрудника</div>
    <input id="hidBadge" class="scan-input" type="text" autocomplete="off" autocapitalize="none" spellcheck="false">
  </div>
  <div class="camera-box">
    <div class="camera-title"><span><b>Вход через камеру</b> · QR бейджа</span><button onclick="toggleCamera()">📷 Камера</button></div>
    <div id="reader"></div>
    <div id="result"></div>
  </div>
</div>
<div class="barcode-zone">
  <div class="barcode-wrap"><svg id="zebraBarcode"></svg></div>
</div>
</div></div>
<script>
let scanner=null,busy=false,hidBuffer='',hidTimer=null;
const hidBadge=document.getElementById('hidBadge');
const ZEBRA_SERVICE_CODE='ONIX-ZEBRA-READY';
let zebraReady=false;

function setScannerReady(){
 zebraReady=true;
 const el=document.getElementById('scannerStatus');
 el.textContent='Zebra готова · сканируй бейдж';
 el.classList.add('ready');
 document.getElementById('result').innerHTML='';
 setTimeout(focusHid,60);
}

function renderZebraBarcode(){
 try{
  JsBarcode('#zebraBarcode',ZEBRA_SERVICE_CODE,{
   format:'CODE128',
   displayValue:false,
   margin:0,
   height:62,
   width:2.2,
   background:'#ffffff',
   lineColor:'#111111'
  });
 }catch(e){}
}

async function enterTerminalFullscreen(){
 try{
  if(!document.fullscreenElement && document.documentElement.requestFullscreen){
   await document.documentElement.requestFullscreen();
  }
 }catch(e){}
}

function focusHid(){
 try{hidBadge.focus({preventScroll:true})}catch(e){try{hidBadge.focus()}catch(_){}}
}

function normalizeHidCode(value){
 let code=String(value||'').replace(/[\\r\\n\\t]/g,'').trim();
 if(/^\\][A-Za-z]\\d/.test(code))code=code.slice(3);
 return code.trim();
}

function submitHidValue(){
 const code=normalizeHidCode(hidBadge.value||hidBuffer);
 hidBadge.value='';
 hidBuffer='';
 clearTimeout(hidTimer);
 if(code===ZEBRA_SERVICE_CODE){
  setScannerReady();
  return;
 }
 if(code){
  if(!zebraReady){
   document.getElementById('result').innerHTML='<div class="err">Сначала подключи Zebra нижним штрихкодом</div>';
   setTimeout(focusHid,80);
   return;
  }
  login(code);
 }
 setTimeout(focusHid,80);
}

async function login(code){
 code=normalizeHidCode(code); if(!code||busy)return; busy=true;
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),8000);
 try{
  document.getElementById('result').innerHTML='<div class="ok">Бейдж считан · выполняю вход…</div>';
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({badge_code:code}),signal:controller.signal});
  const d=await r.json();
  if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Бейдж не принят')+'</div>';return}
  localStorage.setItem('fbs_shift_id',d.shift_id||'');
  document.getElementById('result').innerHTML='<div class="ok">Вход: '+String(d.employee.name)+'</div>';
  setTimeout(()=>location.href='/fbs',250);
 }catch(e){
  const msg=(e&&e.name==='AbortError')?'Сервер не ответил за 8 секунд. Повтори скан.':'Ошибка входа: '+String((e&&e.message)||e||'сеть');
  document.getElementById('result').innerHTML='<div class="err">'+msg+'</div>';
 }finally{
  clearTimeout(timer);
  busy=false;
  if(!scanner)setTimeout(focusHid,80);
 }
}

// HID на iPhone/Safari бывает странным: некоторые сканеры печатают в input,
// но не дают надёжный Enter/input-финиш. Поэтому отправляем код, когда значение
// перестало меняться примерно на 220 мс. Это не зависит от суффикса сканера.
let hidLastValue='',hidStableSince=0,hidPoll=null;

function armHidSubmit(){
 const code=normalizeHidCode(hidBadge.value);
 const now=Date.now();

 if(!code){
  hidLastValue='';
  hidStableSince=now;
  return;
 }

 if(code!==hidLastValue){
  hidLastValue=code;
  hidStableSince=now;
  document.getElementById('result').innerHTML='<div class="ok">Код получен. Жду завершения скана…</div>';
  return;
 }

 if(!busy && now-hidStableSince>=220){
  submitHidValue();
  hidLastValue='';
  hidStableSince=now;
 }
}

hidBadge.addEventListener('input',armHidSubmit);
hidBadge.addEventListener('change',armHidSubmit);
hidBadge.addEventListener('keyup',armHidSubmit);
hidBadge.addEventListener('paste',()=>setTimeout(armHidSubmit,0));

hidBadge.addEventListener('keydown',e=>{
 if(e.key==='Enter'||e.key==='Tab'){
  e.preventDefault();
  enterTerminalFullscreen();
  submitHidValue();
 }
});

// Последний резерв: просто опрашиваем поле. Если Safari показал текст,
// система гарантированно увидит его и отправит на вход.
hidPoll=setInterval(armHidSubmit,80);

// Резерв для HID-сканеров/браузеров, которые всё же шлют события на document.
document.addEventListener('keydown',e=>{
 if(document.activeElement===hidBadge)return;
 if(e.key==='Enter'||e.key==='Tab'){
  const code=normalizeHidCode(hidBuffer); hidBuffer=''; clearTimeout(hidTimer);
  if(code===ZEBRA_SERVICE_CODE){setScannerReady();return}
  if(code){
   if(!zebraReady){
    document.getElementById('result').innerHTML='<div class="err">Сначала подключи Zebra нижним штрихкодом</div>';
    return;
   }
   login(code);
  }
  return;
 }
 if(e.key.length===1){
  hidBuffer+=e.key;
  clearTimeout(hidTimer);
  hidTimer=setTimeout(()=>{hidBuffer=''},1200);
 }
});

window.addEventListener('pageshow',()=>setTimeout(focusHid,120));
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!scanner)setTimeout(focusHid,120)});
setTimeout(focusHid,120);
setTimeout(renderZebraBarcode,40);
async function toggleCamera(){
 await enterTerminalFullscreen();
 const result=document.getElementById('result');

 if(scanner){
  try{await scanner.stop()}catch(e){}
  try{await scanner.clear()}catch(e){}
  scanner=null;
  document.getElementById('reader').innerHTML='';
  result.innerHTML='';
  setTimeout(focusHid,100);
  return;
 }

 if(typeof Html5Qrcode==='undefined'){
  result.innerHTML='<div class="err">Модуль камеры не загрузился. Обнови страницу.</div>';
  return;
 }

 const formats=(typeof Html5QrcodeSupportedFormats!=='undefined')
  ? [Html5QrcodeSupportedFormats.QR_CODE,Html5QrcodeSupportedFormats.CODE_128]
  : undefined;

 scanner=formats
  ? new Html5Qrcode('reader',{formatsToSupport:formats,verbose:false})
  : new Html5Qrcode('reader');

 result.innerHTML='<div class="ok">Камера включена · наведи на QR бейджа</div>';

 try{
  // Максимально простой режим для старого Android: без qrbox/aspectRatio.
  // Сканируется весь кадр, поэтому QR ловится стабильнее.
  await scanner.start(
   {facingMode:'environment'},
   {fps:10,disableFlip:false},
   async txt=>{
    const code=normalizeHidCode(txt);
    if(!code||busy)return;

    result.innerHTML='<div class="ok">QR считан · проверяю бейдж…</div>';

    if(code===ZEBRA_SERVICE_CODE){
     setScannerReady();
     return;
    }

    try{await scanner.stop()}catch(e){}
    try{await scanner.clear()}catch(e){}
    scanner=null;
    await login(code);
   },
   ()=>{}
  );
 }catch(e){
  try{await scanner.clear()}catch(_){}
  scanner=null;
  result.innerHTML='<div class="err">Камера не открылась: '+String((e&&e.message)||e||'ошибка')+'</div>';
  setTimeout(focusHid,100);
 }
}
</script><script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}
</script></body></html>"""


FBS_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="screen-orientation" content="landscape">
<title>FBS · Выработка</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
:root{--bg:#e9eef3;--panel:#f7f9fb;--panel2:#eef3f6;--text:#25313d;--muted:#6b7785;--line:#d7e0e7;--accent:#48647a;--accent2:#dde8ef}
*{box-sizing:border-box}
html,body{margin:0;width:100%;min-height:100%;background:var(--bg);color:var(--text)}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100dvh;-webkit-text-size-adjust:100%}
.wrap{width:100%;max-width:1100px;margin:auto;padding:10px}
.hidden{display:none!important}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px}
.nav{display:flex;gap:6px;margin-bottom:6px}
.nav a{flex:1;display:flex;align-items:center;justify-content:center;min-height:34px;padding:6px 8px;border-radius:9px;background:var(--accent2);color:#314657;border:1px solid #cbd9e2;text-decoration:none;font-size:11px;font-weight:800}
.nav a.active{background:var(--accent);color:#fff;border-color:var(--accent)}
#topbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px;padding:6px 9px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
#topbar .who{font-size:11px;font-weight:800}
#topbar a{font-size:10px;color:#536f83;text-decoration:none;font-weight:800}
#startCard{max-width:520px;margin:20px auto;padding:16px}
#startCard h2{margin:0 0 5px;font-size:18px}
#startCard .muted{font-size:11px;color:var(--muted)}
button{width:100%;border:1px solid transparent;border-radius:9px;background:var(--accent);color:#fff;font-weight:850;padding:10px;font-size:12px;cursor:pointer;margin-top:6px}
button.secondary{background:var(--accent2);color:var(--text);border-color:#cbd9e2}
button.active{background:#d3e3ed;color:#243b4c;border-color:#8ca7b9;box-shadow:inset 0 0 0 1px #8ca7b9}
#work{display:grid;grid-template-columns:minmax(0,1.8fr) minmax(230px,.8fr);gap:6px}
#scanCard{display:flex;flex-direction:column;min-height:0}
.scan-stage{background:var(--panel2);border:1px solid #dce5eb;border-radius:11px;padding:12px;text-align:center}
.scan-stage .label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
#scanStage{font-size:28px;font-weight:900;line-height:1.05;margin-top:3px}
#productInfo{font-size:10px;color:var(--muted);margin-top:5px;min-height:24px;line-height:1.25}
#reader{margin-top:6px;border-radius:10px;overflow:hidden;max-height:150px}
#barcode{width:100%;margin-top:7px;padding:8px;border:1px solid var(--line);border-radius:8px;background:#fff;font-size:11px;outline:none}
#result{margin-top:6px}
.ok,.err{padding:7px;border-radius:8px;font-size:10px}
.ok{background:#e1f1e6;color:#2f6b45}.err{background:#f6e4e4;color:#8a4343}
.tsd-error{position:fixed;inset:0;z-index:5000;background:#b42318;color:#fff;display:none;align-items:center;justify-content:center;padding:20px}
.tsd-error.open{display:flex}.tsd-error-box{width:min(720px,94vw);text-align:center}.tsd-error-icon{font-size:58px;font-weight:900;line-height:1}.tsd-error-title{font-size:34px;font-weight:950;margin-top:8px}.tsd-error-text{font-size:18px;font-weight:750;margin-top:10px;line-height:1.3}.tsd-error button{margin:18px auto 0;max-width:260px;background:#fff;color:#8a1f17;border:0;font-size:18px;padding:12px}
#side{display:flex;flex-direction:column;gap:6px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:5px}
.stat{background:var(--panel2);border:1px solid #dde5eb;border-radius:9px;padding:7px}
.stat.orders{grid-column:1/-1}
.stat .small{font-size:8px;color:var(--muted)}
.stat .big{font-size:20px;font-weight:900;line-height:1.05}
.progress{height:6px;background:#dbe3e9;border-radius:999px;overflow:hidden;margin:5px 0}.progress>div{height:100%;background:#6f8ca1;width:0%}
#remaining{font-size:8px;color:var(--muted)}
#employeeName{display:none}
.ops-title{font-size:9px;color:var(--muted);margin:1px 0 3px;font-weight:800;text-transform:uppercase;letter-spacing:.06em}
.activities{display:grid;grid-template-columns:1fr 1fr;gap:4px}
.activities button{margin:0;padding:7px 4px;font-size:10px;min-height:32px}
.activity-switch{position:relative}
#activityMenu{margin-top:5px}
#activityMenu.hidden{display:none!important}
#activityToggle{margin:0}
#backToPicking{grid-column:1/-1;background:#dfeaf1;color:#25313d;border:1px solid #b9cbd8}
.exit-btn{margin-top:auto}
@media (orientation:landscape) and (max-height:600px){
 body{overflow:hidden}
 .wrap{max-width:none;height:100dvh;overflow:hidden;padding:max(4px,env(safe-area-inset-top)) max(6px,env(safe-area-inset-right)) max(4px,env(safe-area-inset-bottom)) max(6px,env(safe-area-inset-left))}
 .nav{margin-bottom:4px}.nav a{min-height:28px;padding:4px 6px;font-size:9px}
 #topbar{margin-bottom:4px;padding:4px 7px;min-height:28px}#topbar .who{font-size:9px}#topbar a{font-size:9px}
 #work{height:calc(100dvh - 68px);grid-template-columns:minmax(0,1.9fr) minmax(210px,.72fr);gap:4px}
 .card{padding:6px;border-radius:9px}
 .scan-stage{padding:7px}.scan-stage .label{font-size:7px}#scanStage{font-size:23px}#productInfo{font-size:8px;min-height:18px;margin-top:3px}
 #scanCard button{padding:6px;font-size:9px;margin-top:4px}
 #reader{max-height:105px;margin-top:4px}
 #barcode{padding:5px;font-size:9px;margin-top:4px}
 .ok,.err{padding:5px;font-size:8px}
 #side{gap:4px}.stats{gap:3px}.stat{padding:5px}.stat .small{font-size:7px}.stat .big{font-size:17px}.progress{height:5px;margin:3px 0}#remaining{font-size:7px}
 .ops-title{font-size:7px;margin:0 0 2px}.activities{gap:3px}.activities button{padding:5px 2px;font-size:8px;min-height:25px}
 .exit-btn{padding:6px!important;font-size:9px!important}
}
</style>
</head>
<body>
<div class="wrap">
<div class="nav">
  <a href="/">📦 Приёмка</a>
  <a class="active" href="/fbs">🧺 Сборка</a>
  <a href="/move">↔ Перемещение</a>
</div>

<div id="topbar">
  <div class="who"><span id="identity"></span></div>
  <a href="/exit">🚪 Завершить смену</a>
</div>

<div class="card" id="startCard">
  <h2 id="startName">Сотрудник</h2>
  <div class="muted">Нажми кнопку и начинай работу.</div>
  <button onclick="startShift()">Начать смену</button>
</div>

<div id="work" class="hidden">
  <div class="card" id="scanCard">
    <div class="scan-stage" id="scanStageBox">
      <div class="label">Сканируй сейчас</div>
      <div id="scanStage">1. ТОВАР</div>
      <div id="productInfo">После товара система попросит ШК WB.</div>
    </div>

    <button onclick="toggleCamera()">📷 Камера</button>
    <div id="reader" class="hidden"></div>

    <input id="barcode" autocomplete="off" inputmode="text" placeholder="Сканируй ШК">
    <button class="secondary" id="scanButton" onclick="manualScan()">Принять скан</button>
    <button class="secondary hidden" id="resetProduct" onclick="clearPendingProduct()">↩ Сбросить товар</button>
    <div id="result"></div>
  </div>

  <div id="side">
    <div class="card">
      <div id="employeeName">Сотрудник</div>
      <div class="stats">
        <div class="stat orders">
          <div class="small">Заказы</div>
          <div class="big"><span id="orders">0</span> / <span id="target">1600</span></div>
          <div class="progress"><div id="bar"></div></div>
          <div id="remaining"></div>
        </div>
        <div class="stat">
          <div class="small">Заказов/час</div>
          <div class="big" id="speed">0</div>
        </div>
        <div class="stat">
          <div class="small">Сборка</div>
          <div class="big" id="pickTime">0 мин</div>
        </div>
      </div>
    </div>

    <div class="card activity-switch">
      <button class="secondary" id="activityToggle" onclick="toggleActivityMenu()">Другая работа</button>
      <div class="activities hidden" id="activityMenu">
        <button data-a="replenishment" onclick="activity('replenishment')">Пополнение</button>
        <button data-a="shipping" onclick="activity('shipping')">Отгрузка</button>
        <button data-a="pallets" onclick="activity('pallets')">Палеты</button>
        <button data-a="kgt" onclick="activity('kgt')">КГТ</button>
        <button data-a="downtime" onclick="activity('downtime')">Простой</button>
        <button data-a="other" onclick="activity('other')">Другое</button>
        <button id="backToPicking" class="hidden" onclick="activity('picking')">Вернуться к сборке</button>
      </div>
    </div>
  </div>
</div>
</div>
<div id="tsdError" class="tsd-error" onclick="closeTsdError()">
 <div class="tsd-error-box" onclick="event.stopPropagation()">
  <div class="tsd-error-icon">!</div>
  <div class="tsd-error-title">ОШИБКА</div>
  <div id="tsdErrorText" class="tsd-error-text">Скан отклонён</div>
  <button onclick="closeTsdError()">OK</button>
 </div>
</div>
<script>
let shiftId=localStorage.getItem('fbs_shift_id')||'';
let scanner=null,busy=false,audioCtx=null,pendingProduct=null,lastCameraCode='',lastCameraAt=0;

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
function showTsdError(message){
 beep(false);
 document.getElementById('tsdErrorText').textContent=String(message||'Скан отклонён');
 document.getElementById('tsdError').classList.add('open');
}
function closeTsdError(){
 document.getElementById('tsdError').classList.remove('open');
 setTimeout(()=>{const el=document.getElementById('barcode');if(el)el.focus()},60);
}

async function loadIdentity(){
 const r=await fetch('/api/auth/me'),d=await r.json();
 if(!r.ok){location.href='/login';return}
 document.getElementById('identity').textContent=d.employee.name+(d.employee.badge_number?' · бейдж №'+d.employee.badge_number:'');
 document.getElementById('startName').textContent=d.employee.name;
 if(d.employee.role==='manager')document.querySelectorAll('.manager-only').forEach(x=>x.classList.remove('hidden'));
 if(shiftId){
  try{
   await fetch('/api/fbs/activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId,activity_type:'picking'})});
  }catch(e){}
  await refresh();
 }
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
 document.getElementById('pickTime').textContent=Math.round((d.picking_seconds||0)/60)+' мин';
 document.getElementById('speed').textContent=d.orders_per_hour;
 document.getElementById('bar').style.width=Math.min(d.percent,100)+'%';
 document.getElementById('remaining').textContent=d.target_done?'✅ Минималка выполнена':'До 1600: '+d.remaining;
 document.querySelectorAll('[data-a]').forEach(b=>b.classList.toggle('active',b.dataset.a===d.activity));
 const labels={picking:'Другая работа',replenishment:'Сейчас: Пополнение',shipping:'Сейчас: Отгрузка',pallets:'Сейчас: Палеты',kgt:'Сейчас: КГТ',downtime:'Сейчас: Простой',other:'Сейчас: Другое'};
 const toggle=document.getElementById('activityToggle');
 if(toggle)toggle.textContent=labels[d.activity]||'Другая работа';
 const back=document.getElementById('backToPicking');
 if(back)back.classList.toggle('hidden',d.activity==='picking');
}
function updateScanStage(){
 const input=document.getElementById('barcode');
 const reset=document.getElementById('resetProduct');
 if(pendingProduct){
  document.getElementById('scanStage').textContent='2. ШК WB';
  document.getElementById('productInfo').innerHTML='<b>'+esc(pendingProduct.article||'Без артикула')+'</b> · '+esc(pendingProduct.name||'Товар')+'<br>ШК товара: '+esc(pendingProduct.barcode);
  input.placeholder='Теперь сканируй ШК WB';
  reset.classList.remove('hidden');
 }else{
  document.getElementById('scanStage').textContent='1. ТОВАР';
  document.getElementById('productInfo').textContent='После товара система попросит ШК WB.';
  input.placeholder='Сканируй ШК товара';
  reset.classList.add('hidden');
 }
}
function clearPendingProduct(){
 pendingProduct=null;updateScanStage();
 document.getElementById('result').innerHTML='<div class="ok">Товар сброшен. Сканируй товар заново.</div>';
 const el=document.getElementById('barcode');el.value='';el.focus();
}
async function acceptProduct(code){
 const r=await fetch('/api/fbs/product-info',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({barcode:code})});
 const d=await r.json();
 if(!r.ok){showTsdError(d.error||'Товар не найден');return false}
 pendingProduct={barcode:d.barcode,article:d.article||'',name:d.name||''};
 updateScanStage();beep(true);
 document.getElementById('result').innerHTML='<div class="ok">✅ Товар подтверждён. Теперь сканируй ШК WB.</div>';
 return true;
}
async function acceptWb(code){
 const p=pendingProduct;if(!p)return false;
 const r=await fetch('/api/fbs/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
  shift_id:shiftId,barcode:code,product_barcode:p.barcode,article:p.article,product_name:p.name,scan_type:'normal'
 })});
 const d=await r.json();
 if(!r.ok){showTsdError(d.error||'Ошибка ШК WB');return false}
 pendingProduct=null;updateScanStage();beep(true);
 document.getElementById('result').innerHTML='<div class="ok">✅ +1 заказ · '+esc(p.article||p.barcode)+' → '+esc(code)+'</div>';
 await refresh();return true;
}
async function handleScan(code){
 code=(code||'').trim();if(!code||busy||!shiftId)return;
 busy=true;
 try{
  if(pendingProduct)await acceptWb(code);
  else await acceptProduct(code);
 }finally{
  setTimeout(()=>{busy=false;const el=document.getElementById('barcode');el.value='';el.focus()},220);
 }
}
function manualScan(){const el=document.getElementById('barcode');const v=el.value;el.value='';handleScan(v)}
document.getElementById('barcode').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();manualScan()}});
function cameraScan(code){
 const now=Date.now();
 if(code===lastCameraCode && now-lastCameraAt<2500)return;
 lastCameraCode=code;lastCameraAt=now;handleScan(code);
}

function toggleActivityMenu(){
 const menu=document.getElementById('activityMenu');
 if(menu)menu.classList.toggle('hidden');
}
async function activity(type){
 const r=await fetch('/api/fbs/activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId,activity_type:type})});
 const d=await r.json();if(!r.ok){showErr(d.error);return}
 const menu=document.getElementById('activityMenu');
 if(menu)menu.classList.add('hidden');
 await refresh();
}
async function toggleCamera(){
 if(scanner){try{await scanner.stop()}catch(e){}scanner=null;document.getElementById('reader').classList.add('hidden');return}
 if(typeof Html5Qrcode==='undefined'){showErr('Модуль камеры не загрузился');return}
 document.getElementById('reader').classList.remove('hidden');scanner=new Html5Qrcode('reader');
 try{
  await scanner.start({facingMode:'environment'},{fps:15,qrbox:{width:320,height:160}},txt=>cameraScan(txt),()=>{});
 }catch(e){scanner=null;showErr('Камера не открылась: '+e)}
}
function showErr(m){showTsdError(m)}
async function fbsRefreshLoop(){
 if(shiftId&&!document.hidden){try{await refresh()}catch(e){}}
 setTimeout(fbsRefreshLoop,15000);
}
updateScanStage();fetch('/api/warmup',{method:'POST'}).catch(()=>{});loadIdentity();fbsRefreshLoop();
</script><script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  });
}
</script></body></html>"""


EXIT_PAGE = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Завершение смены</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh}
.wrap{max-width:540px;margin:auto;padding:max(16px,env(safe-area-inset-top)) 16px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:18px;padding:18px;box-shadow:0 2px 10px rgba(52,72,91,.07)}
h1{margin:0 0 8px;font-size:24px}.muted{color:#6b7785;font-size:14px;line-height:1.4}.warning{margin-top:12px;padding:12px;background:#fff4dd;border-radius:11px;color:#70531f}
button{width:100%;margin-top:12px;padding:13px;border:0;border-radius:11px;background:#48647a;color:#fff;font-size:16px;font-weight:750}.secondary{display:block;margin-top:10px;text-align:center;color:#536f83;text-decoration:none}
.err,.ok{margin-top:10px;padding:10px;border-radius:10px}.err{background:#f6e4e4;color:#8a4343}.ok{background:#e1f1e6;color:#2f6b45}
#reader{margin-top:12px;overflow:hidden;border-radius:12px}.scanbox{margin-top:14px;padding:14px;border:1px dashed #9fb0bd;border-radius:12px;background:#eef3f6;text-align:center;font-weight:700}

@media (orientation:landscape) and (max-height:600px){
 body{overflow:hidden}
 .wrap{max-width:none;width:100%;height:100dvh;padding:max(6px,env(safe-area-inset-top)) max(8px,env(safe-area-inset-right)) max(6px,env(safe-area-inset-bottom)) max(8px,env(safe-area-inset-left));display:flex;align-items:center;justify-content:center}
 .card{width:min(980px,100%);height:100%;padding:9px 11px;border-radius:12px;display:grid;grid-template-columns:1fr minmax(230px,.75fr);grid-template-rows:auto auto 1fr auto;column-gap:10px;row-gap:5px;overflow:hidden}
 h1{grid-column:1/-1;font-size:18px;margin:0}
 .muted{grid-column:1;font-size:10px;line-height:1.25}
 .warning{grid-column:1;margin:0;padding:8px;font-size:10px;border-radius:8px}
 .scanbox{grid-column:1;margin:0;padding:8px;font-size:11px;border-radius:8px;align-self:start}
 .card>button{grid-column:2;grid-row:2;width:100%;margin:0;padding:8px;font-size:11px}
 #reader{grid-column:2;grid-row:3;margin:0;max-height:150px;border-radius:9px;overflow:hidden}
 #result{grid-column:2;grid-row:4}
 .secondary{grid-column:1;grid-row:4;margin:0;font-size:10px;align-self:end}
 .err,.ok{margin:0;padding:7px;font-size:10px}
}
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
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · QR старшего</title>

<script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:20px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.muted{color:#6b7785;font-size:13px}.top{display:flex;gap:8px;margin-bottom:12px}.top a,.top button{flex:1;padding:10px;border:0;border-radius:10px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}
@media print{.top{display:none}body{background:#fff}.wrap{padding:0}.card{border:0}}
</style></head><body><div class="wrap">
<div class="top"><a href="/manager">← Панель управления</a><button onclick="print()">Печать</button></div>
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
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Терминал выхода</title>
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
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · QR выхода</title>
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


MANAGER_LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Вход руководителя</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:460px;padding:18px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:18px;padding:22px;box-shadow:0 3px 14px rgba(52,72,91,.08)}
h1{margin:0 0 8px;font-size:25px}.muted{color:#6b7785;font-size:14px;line-height:1.45}
input{width:100%;margin-top:16px;padding:14px;border:1px solid #cbd9e2;border-radius:10px;font-size:16px;background:#fff}
button{width:100%;margin-top:10px;padding:13px;border:0;border-radius:10px;background:#48647a;color:#fff;font-size:16px;font-weight:750;cursor:pointer}
.err{margin-top:10px;padding:10px;border-radius:10px;background:#f6e4e4;color:#8a4343}
</style></head><body><div class="wrap"><div class="card">
<h1>🖥️ Панель управления</h1>
<div class="muted">Отдельный вход с компьютера. Сканер и бейдж здесь не нужны.</div>
<input id="password" type="password" autocomplete="current-password" placeholder="Пароль руководителя">
<button onclick="login()">Войти</button>
<div id="result"></div>
</div></div>
<script>
async function login(){
 const password=document.getElementById('password').value;
 const r=await fetch('/api/manager/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})});
 const d=await r.json();
 if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Ошибка входа')+'</div>';return}
 location.href='/manager';
}
document.getElementById('password').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""


STATS_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Панель управления</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100dvh}.sidebar{background:#f7f9fb;border-right:1px solid #d7e0e7;padding:18px 12px;position:sticky;top:0;height:100dvh}.brand{padding:4px 8px 16px;border-bottom:1px solid #e0e7ec;margin-bottom:12px}.brand b{display:block;font-size:18px}.brand span{font-size:11px;color:#6b7785}.side-nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:9px;min-height:44px;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:750;font-size:13px;background:#eef3f6;color:#314657;border:1px solid transparent}.side-nav a:hover{background:#e3ebf0}.side-nav a.active{background:#48647a;color:#fff}.side-nav a.logout{margin-top:12px;background:#f3ecec;color:#794545}
.content{min-width:0;padding:16px}.wrap{max-width:1500px;margin:auto}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(52,72,91,.07)}.head{margin-bottom:12px}.head h1{margin:0;font-size:28px}.small{font-size:12px;color:#6b7785}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}.kpi{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:15px}.kpi .v{font-size:31px;font-weight:850;margin-top:4px}.kpi .l{font-size:12px;color:#6b7785}
.progress-card{margin-bottom:12px}.progress-head{display:flex;align-items:center;justify-content:space-between;gap:14px}.progress-main{display:flex;align-items:baseline;gap:8px}.progress-main .done{font-size:32px;font-weight:850}.progress-main .plan{font-size:18px;color:#6b7785;font-weight:750}.progress{height:14px;background:#dce4ea;border-radius:999px;overflow:hidden;margin:12px 0 7px}.progress>div{height:100%;background:#607f95;width:0%;transition:width .2s}.plan-edit{display:flex;align-items:center;gap:7px}.plan-edit input{width:110px;padding:8px 9px;border:1px solid #cbd9e2;border-radius:9px;font-size:14px;background:#fff}.plan-edit button{border:0;border-radius:9px;background:#48647a;color:#fff;padding:9px 11px;font-weight:750;cursor:pointer}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}.section-title b{font-size:16px}.section-link{font-size:12px;color:#536f83;text-decoration:none;font-weight:750}
.worker{display:grid;grid-template-columns:1.35fr .8fr .8fr .9fr;gap:8px;padding:11px 0;border-bottom:1px solid #dde5eb}.worker:last-child{border:0}.worker b{font-size:14px}.op{font-weight:750}
.receiving-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.mini{background:#eef3f6;border-radius:10px;padding:11px}.mini .n{font-size:24px;font-weight:850}.mini .t{font-size:11px;color:#6b7785;margin-top:2px}
.dev{display:grid;grid-template-columns:80px 110px 140px minmax(0,1fr);gap:10px;padding:10px 0;border-bottom:1px solid #e1e7eb;align-items:center;font-size:12px}.dev:last-child{border:0}.dev .why{font-weight:750}.danger{color:#944747}.warn{color:#916d2d}.empty{padding:16px 0;color:#6b7785;font-size:13px}
.wbbar{display:none;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;padding:10px 14px;background:#f7f9fb;border:1px solid #d7e0e7;border-radius:14px}.wbstate{display:flex;align-items:center;gap:9px;font-size:13px}.dot{width:10px;height:10px;border-radius:50%;background:#a7b2bb;flex:0 0 auto}.dot.ok{background:#4f8a64}.dot.warn{background:#c08a32}.dot.err{background:#ad5757}.wbbar button{border:0;border-radius:9px;background:#48647a;color:#fff;padding:8px 11px;font-weight:750;cursor:pointer}.wbbar button:disabled{opacity:.55;cursor:wait}
.modal{position:fixed;inset:0;background:rgba(20,30,40,.48);display:none;align-items:center;justify-content:center;padding:20px;z-index:1000}.modal.open{display:flex}.modal-card{width:min(430px,96vw);background:#fff;border-radius:18px;padding:20px;box-shadow:0 18px 60px rgba(0,0,0,.22);text-align:center}.modal-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.modal-head h2{margin:0;font-size:21px}.close-btn{border:0;background:#eef3f6;color:#25313d;border-radius:9px;width:38px;height:38px;font-size:22px;cursor:pointer}.qrbox{display:flex;justify-content:center;align-items:center;min-height:290px;margin:14px 0}.ttl{font-size:30px;font-weight:850}.qr-note{font-size:12px;color:#6b7785;margin-top:5px}
@media(max-width:1000px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid #d7e0e7}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a.logout{margin-top:0}.content{padding:12px}.kpis{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}.worker{grid-template-columns:1fr 1fr}.worker>div:first-child{grid-column:1/-1}.progress-head{align-items:flex-start;flex-direction:column}.dev{grid-template-columns:70px 95px 1fr}.dev .object{display:none}}
</style></head><body>
<div class="app">
<aside class="sidebar">
 <div class="brand"><b>📦 ONIX WMS</b><span>Панель управления</span></div>
 <nav class="side-nav">
  <a class="active" href="/manager">🖥️ Главная</a>
  <a href="/fbs-journal">📋 Журнал FBS</a>
  <a href="/history">🔎 История ШК</a>
  <a href="/receiving-monitor">📥 Приёмка</a>
  <a href="/badges">🪪 Бейджи</a>
  <a href="#" onclick="openExitQr();return false">🔐 QR выхода</a>
  <a class="logout" href="#" onclick="managerLogout();return false">🚪 Выйти из ПК</a>
 </nav>
</aside>

<main class="content"><div class="wrap">
 <div class="card head"><h1>🖥️ ONIX WMS</h1><div class="small">Оперативная картина склада. Обновление каждые 10 секунд.</div></div>

 <div id="wbBar" class="wbbar">
  <div class="wbstate"><span id="wbDot" class="dot"></span><div><b id="wbTitle">WB-защита</b><div class="small" id="wbText"></div></div></div>
  <button id="wbSyncBtn" onclick="syncWb()">Обновить WB</button>
 </div>

 <div class="kpis">
  <div class="kpi"><div class="l">WB FBS собрано сегодня</div><div class="v" id="todayOrders">0</div></div>
  <div class="kpi"><div class="l">Осталось по плану</div><div class="v" id="remaining">—</div></div>
  <div class="kpi"><div class="l">Приёмка сегодня, шт.</div><div class="v" id="receivingUnits">0</div></div>
  <div class="kpi"><div class="l">Отклонения сегодня</div><div class="v" id="deviationCount">0</div></div>
 </div>

 <div class="card progress-card">
  <div class="progress-head">
   <div>
    <div class="small">Прогресс WB FBS</div>
    <div class="progress-main"><span class="done" id="progressDone">0</span><span class="plan" id="progressPlan">/ план не задан</span></div>
   </div>
   <div class="plan-edit">
    <input id="planInput" type="number" min="0" step="1" placeholder="План на день">
    <button onclick="savePlan()">Сохранить план</button>
   </div>
  </div>
  <div class="progress"><div id="progressBar"></div></div>
  <div class="small" id="progressText">Задай план на день, чтобы видеть остаток и процент выполнения.</div>
 </div>

 <div class="card" style="margin-bottom:12px">
  <div class="section-title"><b>FBS по часам</b><span class="small">собрано заказов за каждый час</span></div>
  <div id="hourlyChart" style="height:170px;display:flex;align-items:flex-end;gap:5px;padding-top:10px"></div>
 </div>

 <div class="grid2">
  <div class="card">
   <div class="section-title"><b>Сейчас работают</b><span class="small"><span id="active">0</span> активных</span></div>
   <div id="workers">Загрузка…</div>
  </div>

  <div class="card">
   <div class="section-title"><b>Приёмка сегодня</b><a class="section-link" href="/receiving-monitor">Открыть приёмку →</a></div>
   <div class="receiving-grid">
    <div class="mini"><div class="n" id="receivingOps">0</div><div class="t">операций</div></div>
    <div class="mini"><div class="n" id="receivingWorkers">0</div><div class="t">сотрудников</div></div>
    <div class="mini"><div class="n" id="receivingCompleted">0</div><div class="t">документов завершено</div></div>
   </div>
  </div>
 </div>

 <div class="card">
  <div class="section-title"><b>Последние ошибки и отклонения</b><span class="small">только то, что требует внимания</span></div>
  <div id="deviations">Загрузка…</div>
 </div>
</div></main>
</div>

<div id="exitQrModal" class="modal" onclick="if(event.target===this)closeExitQr()">
 <div class="modal-card">
  <div class="modal-head"><h2>🔐 QR завершения смены</h2><button class="close-btn" onclick="closeExitQr()" aria-label="Закрыть">×</button></div>
  <div class="small" style="margin-top:6px">Код меняется каждые 30 секунд.</div>
  <div id="managerExitQr" class="qrbox"></div>
  <div class="ttl"><span id="managerExitTtl">—</span> сек.</div>
  <div class="qr-note">до следующего QR</div>
  <div id="managerExitError" class="small" style="margin-top:8px"></div>
 </div>
</div>

<script>
let currentPlan=0,exitQrTimer=null,lastExitQrCode='';
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return ts}}
function dayBounds(){const n=new Date(),s=new Date(n.getFullYear(),n.getMonth(),n.getDate()),e=new Date(s);e.setDate(e.getDate()+1);return {from:s.toISOString(),to:e.toISOString()}}
function dayKey(){const d=new Date(),y=d.getFullYear(),m=String(d.getMonth()+1).padStart(2,'0'),x=String(d.getDate()).padStart(2,'0');return y+'-'+m+'-'+x}
function activityName(v){return ({picking:'Сборка',replenishment:'Пополнение',receiving:'Приёмка',shipping:'Отгрузка',pallets:'Палеты',kgt:'КГТ',downtime:'Простой',other:'Другое'})[v]||v||'—'}
function activityMinutes(ts){if(!ts)return 0;return Math.max(0,Math.round((Date.now()-new Date(ts).getTime())/60000))}
function deviationLabel(v){return ({product_mismatch:'Не тот товар',wb_order_missing:'Заказ WB не найден',duplicate_wb:'Повторный ШК WB',product_not_found:'Неизвестный товар',logout_denied:'Неверный QR выхода',scan_rejected:'Скан отклонён'})[v]||v}
function deviationClass(v){return ['product_mismatch','duplicate_wb','product_not_found'].includes(v)?'danger':'warn'}
function renderHourly(rows){
 const hours=Array.from({length:24},(_,i)=>({h:i,n:0}));
 rows.forEach(x=>{const d=new Date(x.scanned_at);if(!isNaN(d))hours[d.getHours()].n++});
 const max=Math.max(1,...hours.map(x=>x.n)),box=document.getElementById('hourlyChart');
 box.innerHTML=hours.map(x=>{
  const pct=Math.max(x.n?6:1,Math.round(x.n/max*100));
  return '<div title="'+String(x.h).padStart(2,'0')+':00 · '+x.n+'" style="flex:1;min-width:0;height:100%;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;gap:3px"><div style="font-size:9px;font-weight:800">'+(x.n||'')+'</div><div style="width:100%;height:'+pct+'%;min-height:2px;background:#607f95;border-radius:5px 5px 2px 2px"></div><div style="font-size:8px;color:#6b7785">'+String(x.h).padStart(2,'0')+'</div></div>';
 }).join('');
}

function renderProgress(done){
 document.getElementById('progressDone').textContent=done;
 if(currentPlan>0){
  const rem=Math.max(currentPlan-done,0),pct=Math.min(done/currentPlan*100,100);
  document.getElementById('remaining').textContent=rem;
  document.getElementById('progressPlan').textContent='/ '+currentPlan;
  document.getElementById('progressBar').style.width=pct.toFixed(1)+'%';
  document.getElementById('progressText').textContent='Выполнено '+pct.toFixed(1)+'% · осталось '+rem+' заказов';
 }else{
  document.getElementById('remaining').textContent='—';
  document.getElementById('progressPlan').textContent='/ план не задан';
  document.getElementById('progressBar').style.width='0%';
  document.getElementById('progressText').textContent='Задай план на день, чтобы видеть остаток и процент выполнения.';
 }
}

async function fetchWithTimeout(url,options={},timeoutMs=5000){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),timeoutMs);
 try{return await fetch(url,{...options,signal:controller.signal})}
 finally{clearTimeout(timer)}
}
async function load(){
 const b=dayBounds(),p=new URLSearchParams(b),date=dayKey();
 const [dashR,fbsR,recR,planR,devR,hourR]=await Promise.all([
  fetchWithTimeout('/api/fbs/dashboard'),
  fetchWithTimeout('/api/fbs/overview?'+p.toString()),
  fetchWithTimeout('/api/receiving/overview?'+p.toString()),
  fetchWithTimeout('/api/manager/daily-plan?date='+encodeURIComponent(date)),
  fetchWithTimeout('/api/operations/deviations?'+p.toString()),
  fetchWithTimeout('/api/fbs/hourly?'+p.toString())
 ]);
 if([dashR,fbsR,recR,planR,devR,hourR].some(r=>r.status===403)){location.href='/manager-login';return}
 const d=await dashR.json(),f=await fbsR.json(),r=await recR.json(),plan=await planR.json(),dev=await devR.json(),hour=await hourR.json();

 currentPlan=Number(plan.wb_fbs_plan||0);
 document.getElementById('planInput').value=currentPlan||'';
 document.getElementById('todayOrders').textContent=f.orders||0;
 document.getElementById('receivingUnits').textContent=Math.round(Number(r.units||0));
 document.getElementById('deviationCount').textContent=dev.count||0;
 document.getElementById('active').textContent=d.workers.length;
 renderProgress(Number(f.orders||0));
 renderHourly(hour.rows||[]);

 const box=document.getElementById('workers');
 box.innerHTML=d.workers.length?d.workers.map(x=>'<div class="worker"><div><b>'+esc(x.employee)+'</b><div class="small op">'+esc(activityName(x.activity))+' · '+activityMinutes(x.activity_started_at)+' мин</div></div><div><b>'+x.orders+'</b><div class="small">заказов</div></div><div><b>'+x.orders_per_hour+'</b><div class="small">заказ/ч</div></div><div><b>'+Math.round((x.picking_seconds||0)/60)+' мин</b><div class="small">чистая сборка</div></div></div>').join(''):'<div class="empty">Нет активных смен</div>';

 document.getElementById('receivingOps').textContent=r.operations||0;
 document.getElementById('receivingWorkers').textContent=r.workers||0;
 document.getElementById('receivingCompleted').textContent=r.completed||0;

 document.getElementById('deviations').innerHTML=dev.rows.length?dev.rows.slice(0,8).map(x=>'<div class="dev"><div>'+esc(fmt(x.created_at))+'</div><div><b>'+esc(x.employee)+'</b></div><div class="why '+deviationClass(x.event_type)+'">'+esc(deviationLabel(x.event_type))+'</div><div class="object">'+esc(x.article||x.object_code||x.result||'—')+'</div></div>').join(''):'<div class="empty">Отклонений сегодня нет ✅</div>';
}

async function savePlan(){
 const input=document.getElementById('planInput'),v=Math.max(0,parseInt(input.value||'0',10)||0);
 const r=await fetch('/api/manager/daily-plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({date:dayKey(),wb_fbs_plan:v})});
 const d=await r.json();if(!r.ok){alert(d.error||'Не удалось сохранить план');return}
 currentPlan=v;runDashboardLoad();
}

async function refreshExitQr(){
 try{
  const r=await fetch('/api/admin/supervisor-exit-code',{cache:'no-store'}),d=await r.json();
  if(!r.ok){document.getElementById('managerExitError').textContent=d.error||'Не удалось получить QR';return}
  document.getElementById('managerExitError').textContent='';document.getElementById('managerExitTtl').textContent=d.expires_in;
  if(d.code!==lastExitQrCode){lastExitQrCode=d.code;const box=document.getElementById('managerExitQr');box.innerHTML='';new QRCode(box,{text:d.code,width:280,height:280})}
 }catch(e){document.getElementById('managerExitError').textContent='Ошибка обновления QR'}
}
function openExitQr(){document.getElementById('exitQrModal').classList.add('open');refreshExitQr();if(!exitQrTimer)exitQrTimer=setInterval(refreshExitQr,1000)}
function closeExitQr(){document.getElementById('exitQrModal').classList.remove('open');if(exitQrTimer){clearInterval(exitQrTimer);exitQrTimer=null}}
document.addEventListener('keydown',e=>{if(e.key==='Escape')closeExitQr()});

async function loadWbStatus(){
 try{
  const r=await fetchWithTimeout('/api/wb/status',{cache:'no-store'},5000),d=await r.json(),bar=document.getElementById('wbBar');
  if(!d.configured){bar.style.display='none';return}
  bar.style.display='flex';
  const dot=document.getElementById('wbDot'),title=document.getElementById('wbTitle'),txt=document.getElementById('wbText'),btn=document.getElementById('wbSyncBtn');
  dot.className='dot';btn.disabled=!!d.running;btn.textContent=d.running?'Синхронизация…':'Обновить WB';
  if(d.last_error){dot.classList.add('err');title.textContent='Ошибка WB';txt.textContent=d.last_error}
  else if(d.strict){dot.classList.add('ok');title.textContent='WB-защита включена';txt.textContent='Заказов: '+d.local_orders}
  else{dot.classList.add('warn');title.textContent='WB подключён';txt.textContent='Проверка товара пока тестовая · заказов '+d.local_orders}
 }catch(e){}
}
async function syncWb(){const btn=document.getElementById('wbSyncBtn');btn.disabled=true;btn.textContent='Запускаю…';const r=await fetch('/api/wb/sync',{method:'POST'}),d=await r.json();if(!r.ok){btn.disabled=false;btn.textContent='Обновить WB';document.getElementById('wbText').textContent=d.error||'Ошибка запуска WB';return}loadWbStatus()}
async function managerLogout(){await fetch('/api/manager/logout',{method:'POST'});location.href='/manager-login'}

let dashboardBusy=false,wbStatusBusy=false;
async function runDashboardLoad(){
 if(dashboardBusy||document.hidden)return;
 dashboardBusy=true;
 try{await load()}catch(e){}finally{dashboardBusy=false}
}
async function runWbStatus(){
 if(wbStatusBusy||document.hidden)return;
 wbStatusBusy=true;
 try{await loadWbStatus()}catch(e){}finally{wbStatusBusy=false}
}
async function dashboardLoop(){await runDashboardLoad();setTimeout(dashboardLoop,10000)}
async function wbStatusLoop(){await runWbStatus();setTimeout(wbStatusLoop,15000)}
dashboardLoop();wbStatusLoop();
document.addEventListener('visibilitychange',()=>{if(!document.hidden){runDashboardLoad();runWbStatus()}});
if(sessionStorage.getItem('openExitQr')==='1'){sessionStorage.removeItem('openExitQr');setTimeout(openExitQr,250)}
</script></body></html>"""


RECEIVING_MONITOR_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Приёмка</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100dvh}.sidebar{background:#f7f9fb;border-right:1px solid #d7e0e7;padding:18px 12px;position:sticky;top:0;height:100dvh}.brand{padding:4px 8px 16px;border-bottom:1px solid #e0e7ec;margin-bottom:12px}.brand b{display:block;font-size:18px}.brand span{font-size:11px;color:#6b7785}.side-nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:9px;min-height:44px;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:750;font-size:13px;background:#eef3f6;color:#314657;border:1px solid transparent}.side-nav a:hover{background:#e3ebf0}.side-nav a.active{background:#48647a;color:#fff}.side-nav a.logout{margin-top:12px;background:#f3ecec;color:#794545}
.content{min-width:0;padding:16px}.wrap{max-width:1500px;margin:auto}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(52,72,91,.07);margin-bottom:12px}
h1{margin:0;font-size:28px}.small{font-size:12px;color:#6b7785}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}.kpi{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:15px}.kpi .v{font-size:30px;font-weight:850;margin-top:4px}.kpi .l{font-size:12px;color:#6b7785}
.table-wrap{overflow:auto;max-height:68vh;border:1px solid #dde5eb;border-radius:11px}table{width:100%;border-collapse:collapse;min-width:1050px;background:#fff}th,td{padding:10px 11px;border-bottom:1px solid #e4eaee;text-align:left;font-size:13px;white-space:nowrap}th{position:sticky;top:0;background:#eef3f6;z-index:2;color:#51606e}.article{font-weight:800}.muted{color:#71808d}.qty{font-weight:850}
@media(max-width:1000px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid #d7e0e7}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a.logout{margin-top:0}.content{padding:12px}.kpis{grid-template-columns:1fr 1fr}}
</style></head><body>
<div class="app">
<aside class="sidebar">
 <div class="brand"><b>📦 ONIX WMS</b><span>Панель управления</span></div>
 <nav class="side-nav">
  <a href="/manager">🖥️ Главная</a>
  <a href="/fbs-journal">📋 Журнал FBS</a>
  <a href="/history">🔎 История ШК</a>
  <a class="active" href="/receiving-monitor">📥 Приёмка</a>
  <a href="/badges">🪪 Бейджи</a>
  <a href="/manager#qr" onclick="sessionStorage.setItem('openExitQr','1')">🔐 QR выхода</a>
  <a class="logout" href="#" onclick="managerLogout();return false">🚪 Выйти из ПК</a>
 </nav>
</aside>
<main class="content"><div class="wrap">
 <div class="card"><h1>📥 Приёмка</h1><div class="small">Онлайн-контроль фактически принятых палет и товара. Обновление каждые 10 секунд.</div></div>
 <div class="kpis">
  <div class="kpi"><div class="l">Принято единиц сегодня</div><div class="v" id="units">0</div></div>
  <div class="kpi"><div class="l">Операций приёмки</div><div class="v" id="operations">0</div></div>
  <div class="kpi"><div class="l">Приёмщиков сегодня</div><div class="v" id="workers">0</div></div>
  <div class="kpi"><div class="l">Завершено документов</div><div class="v" id="completed">0</div></div>
 </div>
 <div class="card">
  <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px"><b>Последние принятые позиции</b><span class="small">палетный ШК / ОСЗ / неполная палета</span></div>
  <div class="table-wrap"><table><thead><tr><th>Время</th><th>Сотрудник</th><th>Артикул</th><th>Товар</th><th>ШК / SSCC</th><th>Количество</th><th>Документ</th><th>Тип</th></tr></thead><tbody id="rows"></tbody></table></div>
 </div>
</div></main></div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return ts}}
function bounds(){const n=new Date(),s=new Date(n.getFullYear(),n.getMonth(),n.getDate()),e=new Date(s);e.setDate(e.getDate()+1);return {from:s.toISOString(),to:e.toISOString()}}
function typeLabel(t){return t==='external_pallet'?'ОСЗ / SSCC':t==='scan'?'Палета / товар':t==='complete'?'Завершение':'Операция'}
async function load(){
 const p=new URLSearchParams(bounds()),r=await fetch('/api/receiving/overview?'+p.toString());
 if(r.status===403){location.href='/manager-login';return}
 const d=await r.json();
 document.getElementById('units').textContent=d.units||0;
 document.getElementById('operations').textContent=d.operations||0;
 document.getElementById('workers').textContent=d.workers||0;
 document.getElementById('completed').textContent=d.completed||0;
 document.getElementById('rows').innerHTML=d.recent.length?d.recent.map(x=>'<tr><td>'+esc(fmt(x.created_at))+'</td><td><b>'+esc(x.employee)+'</b></td><td class="article">'+esc(x.article||'—')+'</td><td>'+esc(x.product_name||'—')+'</td><td class="muted">'+esc(x.object_code||'—')+'</td><td class="qty">'+esc(x.quantity??'—')+'</td><td>'+esc(x.document_name||x.document_id||'—')+'</td><td>'+esc(typeLabel(x.event_type))+'</td></tr>').join(''):'<tr><td colspan="8">Сегодня приёмок ещё нет</td></tr>';
}
async function managerLogout(){await fetch('/api/manager/logout',{method:'POST'});location.href='/manager-login'}
let monitorBusy=false;
async function monitorLoop(){
 if(!document.hidden&&!monitorBusy){
  monitorBusy=true;
  try{await load()}catch(e){}finally{monitorBusy=false}
 }
 setTimeout(monitorLoop,10000);
}
monitorLoop();
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!monitorBusy)monitorLoop()});
</script></body></html>"""


FBS_JOURNAL_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Журнал FBS</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.wrap{max-width:1500px;margin:auto;padding:14px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;margin-bottom:10px;box-shadow:0 2px 10px rgba(52,72,91,.07)}
.top{display:flex;align-items:center;justify-content:space-between;gap:10px}.top a{padding:9px 12px;border-radius:9px;background:#dde8ef;color:#314657;text-decoration:none;font-weight:750}
h1{margin:0;font-size:24px}.small{font-size:12px;color:#6b7785}.filters{display:grid;grid-template-columns:1.2fr .8fr .7fr auto;gap:8px;margin-top:12px}
input,select,button{min-height:40px;border:1px solid #cbd9e2;border-radius:9px;background:#fff;padding:8px 10px;font-size:14px;color:#25313d}
button{background:#48647a;color:#fff;font-weight:750;cursor:pointer}
.table-wrap{overflow:auto;max-height:72vh;border:1px solid #dde5eb;border-radius:11px;margin-top:10px}
table{width:100%;border-collapse:collapse;min-width:1000px;background:#fff}th,td{padding:10px 11px;border-bottom:1px solid #e4eaee;text-align:left;font-size:13px;white-space:nowrap}
th{position:sticky;top:0;background:#eef3f6;z-index:2;color:#51606e}.article{font-weight:800}.muted{color:#71808d}
.type-debt{color:#8a5a12;font-weight:750}.type-normal{color:#356446;font-weight:750}
@media(max-width:800px){.filters{grid-template-columns:1fr 1fr}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="wrap">
<div class="card">
 <div class="top"><div><h1>📋 Журнал FBS</h1><div class="small">Кто, какой товар, под какой WB-заказ и во сколько собрал.</div></div><a href="/manager">← Панель управления</a></div>
 <div class="filters">
  <input id="q" placeholder="Артикул, ШК товара или ШК WB">
  <select id="employee"><option value="">Все сотрудники</option></select>
  <select id="type"><option value="">Все типы</option><option value="normal">Обычная сборка</option><option value="debt">Долг WB</option></select>
  <button onclick="load()">Найти</button>
 </div>
 <div class="small" id="summary" style="margin-top:9px"></div>
 <div class="table-wrap"><table><thead><tr><th>Время</th><th>Сотрудник</th><th>Артикул</th><th>Товар</th><th>ШК товара</th><th>ШК WB</th><th>Тип</th></tr></thead><tbody id="rows"></tbody></table></div>
</div>
</div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleString('ru-RU')}catch(e){return ts}}
async function employees(){
 const r=await fetch('/api/stats/employees'),d=await r.json();
 if(!r.ok)return;
 document.getElementById('employee').innerHTML='<option value="">Все сотрудники</option>'+d.employees.map(x=>'<option value="'+esc(x.id)+'">'+esc(x.name)+'</option>').join('');
}
async function load(){
 const q=document.getElementById('q').value.trim(),employee=document.getElementById('employee').value,type=document.getElementById('type').value;
 const p=new URLSearchParams({limit:'500'});if(q)p.set('q',q);if(employee)p.set('employee',employee);if(type)p.set('type',type);
 const r=await fetch('/api/fbs/journal?'+p.toString());if(r.status===403){location.href='/fbs';return}
 const d=await r.json();const box=document.getElementById('rows');
 box.innerHTML=d.rows.length?d.rows.map(x=>'<tr><td>'+esc(fmt(x.scanned_at))+'</td><td><b>'+esc(x.employee)+'</b></td><td class="article">'+esc(x.article||'—')+'</td><td>'+esc(x.product_name||'—')+'</td><td class="muted">'+esc(x.product_barcode||'—')+'</td><td>'+esc(x.wb_barcode)+'</td><td class="'+(x.scan_type==='debt'?'type-debt':'type-normal')+'">'+(x.scan_type==='debt'?'Долг WB':'Обычная')+'</td></tr>').join(''):'<tr><td colspan="7">Ничего не найдено</td></tr>';
 document.getElementById('summary').textContent='Показано записей: '+d.rows.length;
}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')load()});
employees().then(load);
</script></body></html>"""


HISTORY_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · История ШК</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.wrap{max-width:1450px;margin:auto;padding:14px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px}.top a{padding:9px 12px;border-radius:9px;background:#dde8ef;color:#314657;text-decoration:none;font-weight:750}
h1{margin:0;font-size:24px}.small{font-size:12px;color:#6b7785}.search{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:12px}
input,button{min-height:44px;border:1px solid #cbd9e2;border-radius:9px;padding:9px 11px;font-size:15px}button{background:#48647a;color:#fff;font-weight:800;cursor:pointer}
.timeline{margin-top:12px}.event{display:grid;grid-template-columns:155px 130px 160px minmax(0,1fr);gap:10px;padding:11px 0;border-bottom:1px solid #e1e7eb;align-items:center}.event:last-child{border:0}
.time{font-size:12px;color:#6b7785}.who{font-weight:800}.type{font-weight:800}.meta{font-size:12px}.empty{padding:22px 0;color:#6b7785}
@media(max-width:800px){.event{grid-template-columns:1fr 1fr}.meta{grid-column:1/-1}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="wrap">
<div class="card">
 <div class="top"><div><h1>🔎 История ШК</h1><div class="small">Сканируй или введи ШК товара, WB, SSCC, артикул или код объекта.</div></div><a href="/manager">← Панель управления</a></div>
 <div class="search"><input id="q" autofocus placeholder="ШК / WB / SSCC / артикул"><button onclick="load()">Найти</button></div>
 <div id="summary" class="small" style="margin-top:9px"></div>
 <div id="timeline" class="timeline"><div class="empty">Введи код объекта.</div></div>
</div>
</div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleString('ru-RU')}catch(e){return ts}}
function label(t){return ({scan:'Приёмка / скан',external_pallet:'ОСЗ / SSCC',complete:'Документ завершён',start:'Начало смены',end:'Конец смены',switch:'Смена операции',duplicate_wb:'Повторный WB',product_not_found:'Товар не найден',product_mismatch:'Не тот товар',scan_rejected:'Скан отклонён',fbs_scan:'FBS заказ'})[t]||t||'Событие'}
async function load(){
 const q=document.getElementById('q').value.trim();if(!q)return;
 const r=await fetch('/api/history?q='+encodeURIComponent(q));if(r.status===403){location.href='/manager-login';return}
 const d=await r.json();const box=document.getElementById('timeline');
 document.getElementById('summary').textContent='Найдено событий: '+d.rows.length;
 box.innerHTML=d.rows.length?d.rows.map(x=>'<div class="event"><div class="time">'+esc(fmt(x.created_at))+'</div><div class="who">'+esc(x.employee||'—')+'</div><div class="type">'+esc(label(x.event_type))+'</div><div class="meta"><b>'+esc(x.article||x.object_code||'—')+'</b> · '+esc(x.product_name||x.operation_type||'')+(x.quantity!=null?' · '+esc(x.quantity)+' шт.':'')+(x.result?' · '+esc(x.result):'')+'</div></div>').join(''):'<div class="empty">По этому коду история не найдена.</div>';
}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')load()});
</script></body></html>"""


BADGES_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · Бейджи</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}
.wrap{max-width:900px;margin:auto;padding:16px}
.top{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.top a,.top button,.print-one{padding:10px 12px;border:0;border-radius:10px;background:#48647a;color:white;text-decoration:none;font-weight:750;cursor:pointer}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:12px}
.badge{background:#fff;border:1px solid #d7e0e7;border-radius:16px;padding:20px;text-align:center;display:flex;flex-direction:column;align-items:center}
.brand{font-size:11px;color:#6b7785;letter-spacing:.04em}
.name{font-size:22px;font-weight:850;margin-top:8px}
.qr{width:220px;height:220px;max-width:100%;margin:16px auto 12px;display:block;image-rendering:pixelated}
.num{font-size:18px;font-weight:850}
.print-one{margin-top:16px;width:100%}
.status{background:#fff;border:1px solid #d7e0e7;border-radius:16px;padding:18px}
@media print{
 body{background:#fff}
 .top,.print-one{display:none!important}
 .badge{break-inside:avoid;box-shadow:none}
 body.print-single .badge{display:none!important}
 body.print-single .badge.print-target{display:flex!important;margin:0 auto;max-width:360px;border:0}
}
</style></head><body>
<div class="wrap">
 <div class="top"><a href="/stats">← Статистика</a><button type="button" onclick="printAllBadges()">Печать всех</button></div>
 <div id="grid" class="grid"></div>
</div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
async function fetchJson(url,timeoutMs=5000){
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),timeoutMs);
 try{
  const r=await fetch(url,{signal:controller.signal,cache:'no-store'});
  if(r.status===403){location.href='/manager-login';return null}
  if(!r.ok)throw new Error('HTTP '+r.status);
  return await r.json();
 }finally{clearTimeout(timer)}
}
async function load(){
 const g=document.getElementById('grid');
 g.innerHTML='<div class="status"><b>Загружаю бейджи…</b></div>';
 try{
  const d=await fetchJson('/api/admin/badges',5000);
  if(!d)return;
  g.innerHTML=d.employees.map(x=>
   '<div class="badge" id="badge_'+esc(x.id)+'">'+
    '<div class="brand">ONIX WMS</div>'+
    '<div class="name">'+esc(x.name)+'</div>'+
    '<img class="qr" src="'+x.qr_png+'" alt="QR '+esc(x.name)+'">'+
    '<div class="num">№'+esc(x.badge_number)+'</div>'+
    '<button type="button" class="print-one" data-employee-id="'+esc(x.id)+'" onclick="printBadge(this.dataset.employeeId)">Печать</button>'+
   '</div>'
  ).join('');
 }catch(e){
  g.innerHTML='<div class="status"><b>Не удалось загрузить бейджи</b><div style="margin-top:6px;font-size:12px;color:#6b7785">Обнови страницу. Если ошибка повторится, проверь журнал ONIX.</div></div>';
 }
}
function clearPrintSelection(){
 document.body.classList.remove('print-single');
 document.querySelectorAll('.badge.print-target').forEach(el=>el.classList.remove('print-target'));
}
function printAllBadges(){clearPrintSelection();window.print()}
function printBadge(id){
 clearPrintSelection();
 const badge=document.getElementById('badge_'+id);
 if(!badge)return;
 badge.classList.add('print-target');
 document.body.classList.add('print-single');
 window.print();
}
window.addEventListener('afterprint',clearPrintSelection);
load();
</script></body></html>"""


@wms_bp.get("/setup/viktor-8f4c2e91a7b6")
def bootstrap_viktor_badge():
    ensure_db()
    code = make_badge_code("viktor")
    page = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ONIX WMS · Бейдж Виктора</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:18px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:22px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.num{font-size:34px;font-weight:850}.small{font-size:12px;color:#6b7785}.btn{display:block;margin-top:14px;padding:12px;border-radius:11px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}@media print{body{background:#fff}.wrap{padding:0}.btn,.hint{display:none}.card{border:0}}
</style></head><body><div class="wrap"><div class="card">
<div class="small">ONIX WMS</div><div class="num">№1</div><h2>Виктор</h2><div class="small">Руководитель склада · Manager</div>
<div id="qr" class="qr"></div><div class="small">Личный бейдж входа</div>
<a class="btn" href="/login">Перейти ко входу</a><div class="small hint" style="margin-top:12px">Сохрани или распечатай этот QR. Он предназначен для входа Виктора в ONIX WMS.</div>
</div></div><script>new QRCode(document.getElementById('qr'),{text:"__VIKTOR_BADGE_CODE__",width:260,height:260});</script></body></html>"""
    return page


@wms_bp.get("/login")
def login_page():
    ensure_db()
    if get_session_employee():
        return redirect("/fbs")
    return LOGIN_PAGE


def normalize_badge_input(value):
    code = str(value or "").strip()
    # HID scanners can prepend AIM symbology identifiers such as ]C0 / ]C1.
    if len(code) >= 3 and code[0] == "]" and code[1].isalpha() and code[2].isdigit():
        code = code[3:]
    return code.replace("\r", "").replace("\n", "").replace("\t", "").strip()


@wms_bp.post("/api/auth/login")
def auth_login():
    ensure_db()
    data = request.get_json(force=True)
    badge_code = normalize_badge_input(data.get("badge_code", ""))
    if not badge_code:
        return jsonify(error="Пустой код бейджа"), 400

    row = query_one(
        "SELECT id, name, role, badge_number FROM employees WHERE (badge_code = ? OR scanner_code = ?) AND active = 1",
        (badge_code, badge_code),
    )

    # Простые стабильные форматы для TSD. Они не зависят от смены секрета
    # и спасают вход, если старый распечатанный бейдж пережил деплой/ротацию ключей.
    if not row:
        employee_id = ""
        badge_number = None
        upper = badge_code.upper()
        if upper.startswith("ONIX-EMP-"):
            employee_id = badge_code[len("ONIX-EMP-"):].strip()
        elif upper.startswith("ONIX-BADGE-"):
            try:
                badge_number = int(badge_code[len("ONIX-BADGE-"):].strip())
            except Exception:
                badge_number = None
        elif badge_code.isdigit() and len(badge_code) <= 4:
            try:
                badge_number = int(badge_code)
            except Exception:
                badge_number = None

        if employee_id:
            row = query_one(
                "SELECT id, name, role, badge_number FROM employees WHERE id = ? AND active = 1",
                (employee_id,),
            )
        elif badge_number is not None:
            row = query_one(
                "SELECT id, name, role, badge_number FROM employees WHERE badge_number = ? AND active = 1",
                (badge_number,),
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
    if not manager_access():
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
    allowed = {"picking", "replenishment", "receiving", "movement", "shipping", "pallets", "kgt", "downtime", "other"}
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


@wms_bp.get("/manager-login")
def manager_login_page():
    if manager_access():
        return redirect("/manager")
    return MANAGER_LOGIN_PAGE


@wms_bp.post("/api/manager/login")
def manager_login_api():
    ip = _manager_client_ip()
    state = _manager_rate_state(ip)
    if state["blocked"]:
        return jsonify(
            error="Слишком много неверных попыток. Повтори вход позже.",
            retry_after=state["remaining"],
        ), 429

    data = request.get_json(silent=True) or {}
    password = str(data.get("password", ""))
    if not MANAGER_WEB_PASSWORD:
        return jsonify(error="Пароль руководителя не настроен в Render"), 503

    if not hmac.compare_digest(password, MANAGER_WEB_PASSWORD):
        failed = _manager_register_failed_login(ip)
        if int(failed.get("attempts", 0)) >= MANAGER_LOGIN_MAX_ATTEMPTS:
            return jsonify(
                error="Слишком много неверных попыток. Вход временно заблокирован.",
                retry_after=MANAGER_LOGIN_BLOCK_SECONDS,
            ), 429
        remaining_attempts = max(MANAGER_LOGIN_MAX_ATTEMPTS - int(failed.get("attempts", 0)), 0)
        return jsonify(
            error="Неверный пароль",
            attempts_left=remaining_attempts,
        ), 403

    _manager_clear_failed_logins(ip)
    session.clear()
    session["manager_web"] = True
    return jsonify(ok=True)


@wms_bp.post("/api/manager/logout")
def manager_logout_api():
    session.pop("manager_web", None)
    return jsonify(ok=True)


@wms_bp.get("/manager")
def manager_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return STATS_PAGE


@wms_bp.get("/stats")
def stats_page():
    return redirect("/manager")


@wms_bp.get("/receiving-monitor")
def receiving_monitor_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return RECEIVING_MONITOR_PAGE


@wms_bp.get("/fbs-journal")
def fbs_journal_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return FBS_JOURNAL_PAGE


@wms_bp.get("/fbs-manager")
def old_manager_page():
    return redirect("/stats")


@wms_bp.get("/history")
def history_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return HISTORY_PAGE


@wms_bp.get("/api/history")
def history_api():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    q = str(request.args.get("q", "")).strip()
    if not q:
        return jsonify(rows=[])

    like = "%" + q + "%"
    rows = []

    scan_rows = query_all(
        """
        SELECT s.scanned_at, e.name, s.article, s.product_name, s.product_barcode,
               s.barcode, COALESCE(s.scan_type,'normal')
        FROM scans s
        JOIN employees e ON e.id = s.employee_id
        WHERE s.barcode LIKE ? OR COALESCE(s.product_barcode,'') LIKE ?
           OR COALESCE(s.article,'') LIKE ? OR COALESCE(s.product_name,'') LIKE ?
        ORDER BY s.scanned_at DESC
        LIMIT 200
        """,
        (like, like, like, like),
    )
    for r in scan_rows:
        rows.append({
            "created_at": r[0], "employee": r[1], "operation_type": "fbs",
            "event_type": "fbs_scan", "object_code": r[5], "quantity": 1,
            "result": r[6], "article": r[2], "product_name": r[3],
        })

    event_rows = query_all(
        """
        SELECT oe.created_at, e.name, oe.operation_type, oe.event_type,
               oe.object_code, oe.quantity, oe.result, oe.article, oe.product_name
        FROM operation_events oe
        JOIN employees e ON e.id = oe.employee_id
        WHERE COALESCE(oe.object_code,'') LIKE ? OR COALESCE(oe.article,'') LIKE ?
           OR COALESCE(oe.product_name,'') LIKE ? OR COALESCE(oe.document_name,'') LIKE ?
           OR COALESCE(oe.result,'') LIKE ?
        ORDER BY oe.created_at DESC
        LIMIT 300
        """,
        (like, like, like, like, like),
    )
    for r in event_rows:
        rows.append({
            "created_at": r[0], "employee": r[1], "operation_type": r[2],
            "event_type": r[3], "object_code": r[4], "quantity": r[5],
            "result": r[6], "article": r[7], "product_name": r[8],
        })

    rows.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return jsonify(rows=rows[:400])


@wms_bp.get("/api/fbs/hourly")
def fbs_hourly_api():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    date_from = str(request.args.get("from", "")).strip()
    date_to = str(request.args.get("to", "")).strip()
    if not date_from or not date_to:
        now = datetime.now(timezone.utc)
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        date_to = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
    rows = query_all(
        "SELECT scanned_at FROM scans WHERE scanned_at >= ? AND scanned_at < ? ORDER BY scanned_at",
        (date_from, date_to),
    )
    return jsonify(rows=[{"scanned_at": r[0]} for r in rows])


@wms_bp.get("/badges")
def badges_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return BADGES_PAGE


@wms_bp.get("/api/admin/badges")
def admin_badges():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    import qrcode

    rows = query_all(
        "SELECT id, name, badge_number, badge_code FROM employees WHERE active = 1 ORDER BY badge_number, name"
    )
    employees = []
    for employee_id, name, badge_number, badge_code in rows:
        code = str(badge_code or "").strip()
        if not code:
            code = make_badge_code(employee_id)
            execute("UPDATE employees SET badge_code = ? WHERE id = ?", (code, employee_id))

        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=8,
            border=4,
        )
        qr.add_data(code)
        qr.make(fit=True)
        image = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        qr_png = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

        employees.append({
            "id": employee_id,
            "name": name,
            "badge_number": badge_number,
            "qr_png": qr_png,
        })

    return jsonify(employees=employees)


@wms_bp.get("/api/stats/employees")
def stats_employees():
    ensure_db()
    if not manager_access():
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


@wms_bp.get("/api/manager/daily-plan")
def manager_daily_plan_get():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    date_key = str(request.args.get("date", "")).strip()
    if not date_key:
        return jsonify(error="Не указана дата"), 400
    row = query_one(
        "SELECT wb_fbs_plan, updated_at, updated_by FROM daily_targets WHERE date_key = ?",
        (date_key,),
    )
    return jsonify(
        date=date_key,
        wb_fbs_plan=int(row[0] or 0) if row else 0,
        updated_at=row[1] if row else None,
        updated_by=row[2] if row else None,
    )


@wms_bp.post("/api/manager/daily-plan")
def manager_daily_plan_set():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    data = request.get_json(silent=True) or {}
    date_key = str(data.get("date", "")).strip()
    try:
        plan = int(data.get("wb_fbs_plan", 0) or 0)
    except Exception:
        return jsonify(error="План должен быть целым числом"), 400
    if not date_key:
        return jsonify(error="Не указана дата"), 400
    if plan < 0:
        return jsonify(error="План не может быть отрицательным"), 400

    updated_by = "ПК"
    emp = get_session_employee()
    if emp:
        updated_by = emp["name"]

    if query_one("SELECT date_key FROM daily_targets WHERE date_key = ?", (date_key,)):
        execute(
            "UPDATE daily_targets SET wb_fbs_plan = ?, updated_at = ?, updated_by = ? WHERE date_key = ?",
            (plan, now_iso(), updated_by, date_key),
        )
    else:
        execute(
            "INSERT INTO daily_targets (date_key, wb_fbs_plan, updated_at, updated_by) VALUES (?, ?, ?, ?)",
            (date_key, plan, now_iso(), updated_by),
        )
    return jsonify(ok=True, date=date_key, wb_fbs_plan=plan)


@wms_bp.get("/api/operations/deviations")
def operations_deviations():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    date_from = str(request.args.get("from", "")).strip()
    date_to = str(request.args.get("to", "")).strip()
    if not date_from or not date_to:
        now = datetime.now(timezone.utc)
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        date_to = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

    event_types = (
        "product_mismatch",
        "wb_order_missing",
        "duplicate_wb",
        "product_not_found",
        "logout_denied",
        "scan_rejected",
    )
    placeholders = ",".join(["?"] * len(event_types))
    params = tuple(event_types) + (date_from, date_to)
    rows = query_all(
        """
        SELECT oe.created_at, e.name, oe.operation_type, oe.event_type,
               oe.object_code, oe.article, oe.product_name, oe.result
        FROM operation_events oe
        JOIN employees e ON e.id = oe.employee_id
        WHERE oe.event_type IN (""" + placeholders + """)
          AND oe.created_at >= ? AND oe.created_at < ?
        ORDER BY oe.created_at DESC
        LIMIT 50
        """,
        params,
    )
    total_row = query_one(
        """
        SELECT COUNT(*)
        FROM operation_events
        WHERE event_type IN (""" + placeholders + """)
          AND created_at >= ? AND created_at < ?
        """,
        params,
    )
    return jsonify(
        count=int(total_row[0] if total_row else 0),
        rows=[
            {
                "created_at": r[0],
                "employee": r[1],
                "operation_type": r[2],
                "event_type": r[3],
                "object_code": r[4],
                "article": r[5],
                "product_name": r[6],
                "result": r[7],
            }
            for r in rows
        ],
    )


@wms_bp.get("/api/receiving/overview")
def receiving_overview_api():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    date_from = str(request.args.get("from", "")).strip()
    date_to = str(request.args.get("to", "")).strip()
    if not date_from or not date_to:
        now = datetime.now(timezone.utc)
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        date_to = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

    totals = query_one(
        """
        SELECT COALESCE(SUM(CASE WHEN event_type IN ('scan','external_pallet') THEN quantity ELSE 0 END),0),
               SUM(CASE WHEN event_type IN ('scan','external_pallet') THEN 1 ELSE 0 END),
               COUNT(DISTINCT CASE WHEN event_type IN ('scan','external_pallet') THEN employee_id END),
               SUM(CASE WHEN event_type = 'complete' THEN 1 ELSE 0 END)
        FROM operation_events
        WHERE operation_type = 'receiving' AND created_at >= ? AND created_at < ?
        """,
        (date_from, date_to),
    )

    rows = query_all(
        """
        SELECT oe.created_at, e.name, oe.article, oe.product_name, oe.object_code,
               oe.quantity, oe.result, oe.document_name, oe.event_type
        FROM operation_events oe
        JOIN employees e ON e.id = oe.employee_id
        WHERE oe.operation_type = 'receiving'
          AND oe.event_type IN ('scan','external_pallet')
          AND oe.created_at >= ? AND oe.created_at < ?
        ORDER BY oe.created_at DESC
        LIMIT 200
        """,
        (date_from, date_to),
    )

    return jsonify(
        units=float(totals[0] or 0),
        operations=int(totals[1] or 0),
        workers=int(totals[2] or 0),
        completed=int(totals[3] or 0),
        recent=[
            {
                "created_at": r[0],
                "employee": r[1],
                "article": r[2],
                "product_name": r[3],
                "object_code": r[4],
                "quantity": r[5],
                "document_id": r[6],
                "document_name": r[7],
                "event_type": r[8],
            }
            for r in rows
        ],
    )


@wms_bp.get("/api/fbs/journal")
def fbs_journal_api():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    q = str(request.args.get("q", "")).strip()
    employee_id = str(request.args.get("employee", "")).strip()
    scan_type = str(request.args.get("type", "")).strip()
    try:
        limit = max(1, min(int(request.args.get("limit", "500")), 1000))
    except Exception:
        limit = 500

    where = ["1=1"]
    params = []
    if employee_id:
        where.append("s.employee_id = ?")
        params.append(employee_id)
    if scan_type in ("normal", "debt"):
        where.append("COALESCE(s.scan_type, 'normal') = ?")
        params.append(scan_type)
    if q:
        like = "%" + q + "%"
        where.append("(s.barcode LIKE ? OR COALESCE(s.product_barcode,'') LIKE ? OR COALESCE(s.article,'') LIKE ? OR COALESCE(s.product_name,'') LIKE ?)")
        params.extend([like, like, like, like])

    params.append(limit)
    rows = query_all(
        """
        SELECT s.scanned_at, e.name, s.article, s.product_name, s.product_barcode,
               s.barcode, COALESCE(s.scan_type, 'normal')
        FROM scans s
        JOIN employees e ON e.id = s.employee_id
        WHERE """ + " AND ".join(where) + """
        ORDER BY s.scanned_at DESC
        LIMIT ?
        """,
        tuple(params),
    )
    return jsonify(rows=[
        {
            "scanned_at": r[0],
            "employee": r[1],
            "article": r[2],
            "product_name": r[3],
            "product_barcode": r[4],
            "wb_barcode": r[5],
            "scan_type": r[6] or "normal",
        }
        for r in rows
    ])


@wms_bp.get("/manifest.webmanifest")
def pwa_manifest():
    manifest = {
        "id": "/fbs",
        "name": "ONIX WMS · FBS терминал",
        "short_name": "ONIX FBS",
        "description": "Рабочий FBS-терминал ONIX WMS",
        "start_url": "/fbs",
        "scope": "/",
        "display": "fullscreen",
        "display_override": ["fullscreen", "standalone"],
        "orientation": "landscape",
        "background_color": "#e9eef3",
        "theme_color": "#48647a",
        "icons": [
            {"src": "/pwa-icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
            {"src": "/pwa-icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}
        ]
    }
    response = Response(json.dumps(manifest, ensure_ascii=False), mimetype="application/manifest+json")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


_PWA_ICON_192 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAYAAABS3GwHAAADOUlEQVR42u3dQU7yUBiGUWi6OBy6Bxy5AxwYByzBEYtwyPJwZKKJIqXQ9rvvOUPzx/i339PbAl7Xm+3utIJQnUOAAEAAIAAQAAgABAACAAGAAEAAIAAQAAgABAACAAGAAEAAIAAQAAgABAACAAGAAEAAIAAQAAgABAACAAHAPfVVftCP9zdnq5jH59fF/4zrJf+JJEMvhsgADL4QYp8BDH/blnZ+OweH5PPcOSgkn+/OwSD5vHcOAsnn3xthROuS68cqYAXACgACcPtD4G2QFQArAAgABAACAAGAAEAAIAAQAAgABAACAAGAAEAAUE/fwn+iwiasrar+i02d4Sf5+LsFwjMACAAEAAIAAYAAQAAggMWyxaLjP0bvJGAFAAGAAEAAIAAQAAgABAACAAGAAEAAIAAozMZYjGJjLMMfzcZYIAAQAAgABAACAAGAAEAAd2JPIMd/DBtjYQUAAYAAQAAgABAACADa1TsEmS75Ta6E91cEYOgv+vetxiAAgz/oe7QWggAMfnQIHoIN/yK/vxWAxQ9mC6uBjbG4yfGvGoGNsYg+D54BiCYAolcBARAdgQAY5HjYuwUie/j/i6DSKiAArrryHw/7JlYDAXCXW6Iqq4AAGHXf//D0YgWYkz2BDP8YNsYqaKrbi7H3+BU+IuEWiKuGv4WrvwCIHn4BGOrBWhp+AYQN/yURtP7QK4DwK/+5AU8bfgGE3vb89vXWPuMjAM5etb8PfNJDrwBE8GPwk4dfAEUNfXPp2iEeO/wV3qAUgJUg8sovABHED78Agm6Dph7uKp/PEoCVIPbqL4DQVeCvQb/V8Ff6dK6tEa0E0awAwatA8r2/AEQQP/wCwC2QQ2AVSL36C0AE0cO/WnkVqNkIpvjF+RY2I5htBbCvf+3hvPX3n2serABWg7irvgCEED/4X9ab7e7k1YM8S/pL8XPeDlsBPCNEm/1lUA/DVqLoAERg+OMDEIHhjw9ABIY/PgARGP6pzf4y6DleqTD40QGIwdALAJKeAUAAIAAQAAgABAACAAGAAEAAIAAQAAgABAACAAGAAEAAIAAQAAgABAACQAAgABAACAAEAAIAAYAAQAAgAGjLJzuHA2/RGO37AAAAAElFTkSuQmCC"
_PWA_ICON_512 = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAYAAAD0eNT6AAAKgklEQVR42u3dPXLbSBCAUdGFw8kh7kBHuAEdsBD4CIh0CIQ+Hh25iuWSVfzBENPd76VbK9uzxPQ3A1l7eD+eLm8AQCnfLAEACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAEAAAgAAAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAAAgAAEAAAgAAAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAACATQ2WgGvjdLYIkNi6zBYBNwAY/uA5RwBgUwA87wgAbAaA5x4BgE0A8PwjAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAnVmX2SKA5x8BgE0A8NwjALAZAJ53BAA2BcBzTg6DJcDmAOAGAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAIAABAAAAAeQ2WgGvjdLYIkNi6zBYBNwAY/uA5RwBgUwA87wgAbAaA5x4BgE0A8PwjAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAACAAAAABAAAIAABAAAAAAgAAEAAAgACgb+syWwTw/CMAsAkAnnsEADYDwPOOAMCmAHjOyWGwBNgcANwAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAAFgCQBAAAAAAgAAEAAAgAAAAGIaLEEe6zJbBKC5cTpbhAQO78fTxTIY+ACCQABg6AOIAQGAoQ8gBgQABj+AEBAAGPwAQiAefw3Q8Aewv7kBwIMB4DbADQCGP4B9TwDgIQCw/+XgFYAPPsDuvBJwA2D4A9gXEQA+5AD2RwSADzeAfRIBAAAIAFULYL9EAPgwA9g3EQA+xAD2TwQAAAgA1CuAfVQA4EMLYD8VAACAAECtAthXBQAAIABUqkoFsL8KAABAAAAAAiAZ11MA9lkBAAAIAABAAKTjWgrAfisAAAABAAAIAABAAMTnfRSAfVcAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAAAsASAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAACATQ2WgGvjdLYIkNi6zBYBNwAY/uA5RwBgUwA87wgAbAaA5x4BgE0A8PwjAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAnVmX2SKA5x8BgE0A8NwjALAZAJ53BAA2BcBzTg6DJcDmAOAGAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAABAAAIAAAAAEAAAgAAAAAQAACAAAQAAAAAIAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAACAAAAABAAAIAABAAAAAAgAAEAAAgAAAAAQAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAAACAAAQAACAAAAABAAAIAAAAAEAAAgAAEAAAIAAAAAEAAAgAAAAAQAABPUHuvmdqNvFwcMAAAAASUVORK5CYII="


@wms_bp.get("/pwa-icon-192.png")
def pwa_icon_192():
    return Response(base64.b64decode(_PWA_ICON_192), mimetype="image/png")


@wms_bp.get("/pwa-icon-512.png")
def pwa_icon_512():
    return Response(base64.b64decode(_PWA_ICON_512), mimetype="image/png")


@wms_bp.get("/sw.js")
def pwa_service_worker():
    js = """
const CACHE = 'onix-pwa-v2';
self.addEventListener('install', event => {
  self.skipWaiting();
});
self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
});
self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});
"""
    response = Response(js, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response



MOVE_PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3">
<link rel="manifest" href="/manifest.webmanifest">
<meta name="mobile-web-app-capable" content="yes">
<meta name="screen-orientation" content="landscape">
<title>ONIX WMS · Перемещение</title>
<style>
:root{--bg:#e9eef3;--panel:#f7f9fb;--panel2:#eef3f6;--text:#25313d;--muted:#6b7785;--line:#d7e0e7;--accent:#48647a;--accent2:#dde8ef}
*{box-sizing:border-box}
html,body{margin:0;width:100%;min-height:100%;background:var(--bg);color:var(--text)}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;min-height:100dvh}
.wrap{width:100%;max-width:1100px;margin:auto;padding:10px}
.nav{display:flex;gap:6px;margin-bottom:6px}
.nav a{flex:1;display:flex;align-items:center;justify-content:center;min-height:34px;padding:6px 8px;border-radius:9px;background:var(--accent2);color:#314657;border:1px solid #cbd9e2;text-decoration:none;font-size:11px;font-weight:800}
.nav a.active{background:var(--accent);color:#fff;border-color:var(--accent)}
#topbar{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:6px;padding:6px 9px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
#topbar .who{font-size:11px;font-weight:800}
#topbar a{font-size:10px;color:#536f83;text-decoration:none;font-weight:800}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
.stage{min-height:220px;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;background:var(--panel2);border:1px solid #dce5eb;border-radius:11px}
.kicker{font-size:9px;color:var(--muted);font-weight:900;letter-spacing:.1em}
h1{font-size:30px;margin:4px 0 6px}
.muted{font-size:11px;color:var(--muted)}
@media (orientation:landscape) and (max-height:600px){
 body{overflow:hidden}
 .wrap{max-width:none;height:100dvh;overflow:hidden;padding:max(4px,env(safe-area-inset-top)) max(6px,env(safe-area-inset-right)) max(4px,env(safe-area-inset-bottom)) max(6px,env(safe-area-inset-left))}
 .nav{margin-bottom:4px}.nav a{min-height:28px;padding:4px 6px;font-size:9px}
 #topbar{margin-bottom:4px;padding:4px 7px;min-height:28px}#topbar .who{font-size:9px}#topbar a{font-size:9px}
 .card{height:calc(100dvh - 68px);padding:6px;border-radius:9px}
 .stage{height:100%;min-height:0}
 h1{font-size:26px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="nav">
    <a href="/">📦 Приёмка</a>
    <a href="/fbs">🧺 Сборка</a>
    <a class="active" href="/move">↔ Перемещение</a>
  </div>
  <div id="topbar">
    <div class="who"><span id="identity"></span></div>
    <a href="/exit">🚪 Завершить смену</a>
  </div>
  <div class="card">
    <div class="stage">
      <div class="kicker">ПЕРЕМЕЩЕНИЕ</div>
      <h1>Перемещение</h1>
      <div class="muted">Отдельный рабочий модуль. Логику перемещения разберём следующим шагом.</div>
    </div>
  </div>
</div>
<script>
async function loadMove(){
  const r=await fetch('/api/auth/me'),d=await r.json();
  if(!r.ok){location.href='/login';return}
  document.getElementById('identity').textContent=d.employee.name+(d.employee.badge_number?' · бейдж №'+d.employee.badge_number:'');
  const shiftId=localStorage.getItem('fbs_shift_id')||'';
  if(shiftId){
    try{
      await fetch('/api/fbs/activity',{
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({shift_id:shiftId,activity_type:'movement'})
      });
    }catch(e){}
  }
}
loadMove();
</script>
</body>
</html>"""


@wms_bp.get("/move")
def move_page():
    ensure_db()
    if not get_session_employee():
        return redirect("/login")
    return MOVE_PAGE


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
    product_barcode = str(data.get("product_barcode", "")).strip()
    article = str(data.get("article", "")).strip()
    product_name = str(data.get("product_name", "")).strip()
    scan_type = str(data.get("scan_type", "normal")).strip().lower()
    if not barcode:
        return jsonify(error="Пустой ШК WB"), 400
    if not product_barcode:
        return jsonify(error="Сначала отсканируй товар"), 400
    if scan_type not in ("normal", "debt"):
        scan_type = "normal"

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
        log_event(
            shift[0],
            "fbs",
            "scan_rejected",
            shift_id=shift_id,
            object_code=barcode,
            result="activity=" + str((active or {}).get("type") or "none"),
            article=article or None,
            product_name=product_name or None,
        )
        return jsonify(error="Сейчас включена не сборка. Переключи операцию на «Сборка»."), 409

    wb_order_id = None
    expected = query_one(
        "SELECT order_id, article, skus_json FROM wb_orders WHERE wb_barcode = ? LIMIT 1",
        (barcode,),
    )
    if expected:
        wb_order_id = str(expected[0])
        expected_article = str(expected[1] or "").strip()
        try:
            expected_skus = [str(x).strip() for x in json.loads(expected[2] or "[]") if str(x).strip()]
        except Exception:
            expected_skus = []

        sku_ok = product_barcode in expected_skus if expected_skus else False
        article_ok = bool(expected_article and article and expected_article == article)

        if WB_STRICT_MATCHING and not (sku_ok or (not expected_skus and article_ok)):
            log_event(
                shift[0],
                "fbs",
                "product_mismatch",
                shift_id=shift_id,
                object_code=barcode,
                result=f"scanned={article or product_barcode};expected={expected_article or ','.join(expected_skus)}",
                article=article or None,
                product_name=product_name or None,
            )
            expected_text = expected_article or (", ".join(expected_skus[:3]) if expected_skus else "неизвестный товар")
            return jsonify(
                error="❌ НЕ ТОТ ТОВАР. Для этого WB-заказа ожидается: " + expected_text,
                expected_article=expected_article,
                expected_skus=expected_skus,
            ), 409
    elif WB_STRICT_MATCHING:
        log_event(
            shift[0],
            "fbs",
            "wb_order_missing",
            shift_id=shift_id,
            object_code=barcode,
            result="not-synced",
            article=article or None,
            product_name=product_name or None,
        )
        return jsonify(
            error="WB-заказ не найден в локальном справочнике. На ПК обнови синхронизацию WB."
        ), 409

    duplicate = query_one(
        "SELECT s.id, e.name FROM scans s JOIN employees e ON e.id = s.employee_id WHERE s.barcode = ? LIMIT 1",
        (barcode,),
    )
    if duplicate:
        log_event(
            shift[0],
            "fbs",
            "duplicate_wb",
            shift_id=shift_id,
            object_code=barcode,
            result="already=" + str(duplicate[1]),
            article=article or None,
            product_name=product_name or None,
        )
        return jsonify(error="Этот заказ уже был засчитан: " + str(duplicate[1])), 409

    try:
        execute(
            """
            INSERT INTO scans
            (id, shift_id, employee_id, barcode, product_barcode, article, product_name, wb_order_id, scan_type, scanned_at, marketplace)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'WB')
            """,
            (str(uuid.uuid4()), shift_id, shift[0], barcode, product_barcode, article or None, product_name or None, wb_order_id, scan_type, now_iso()),
        )
    except Exception:
        duplicate = query_one(
            "SELECT e.name FROM scans s JOIN employees e ON e.id = s.employee_id WHERE s.barcode = ? LIMIT 1",
            (barcode,),
        )
        if duplicate:
            return jsonify(error="Этот заказ уже был засчитан: " + str(duplicate[0])), 409
        raise

    log_event(
        shift[0],
        "fbs",
        "scan",
        shift_id=shift_id,
        object_code=barcode,
        quantity=1,
        result=(article or product_barcode) + "|" + scan_type,
    )
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


@wms_bp.get("/api/fbs/overview")
def fbs_overview():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    date_from = str(request.args.get("from", "")).strip()
    date_to = str(request.args.get("to", "")).strip()
    if not date_from or not date_to:
        now = datetime.now(timezone.utc)
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        date_to = now.replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()

    totals = query_one(
        """
        SELECT COUNT(*), COUNT(DISTINCT employee_id),
               COUNT(DISTINCT CASE WHEN article IS NOT NULL AND article <> '' THEN article END)
        FROM scans
        WHERE scanned_at >= ? AND scanned_at < ?
        """,
        (date_from, date_to),
    )

    recent_rows = query_all(
        """
        SELECT s.scanned_at, e.name, s.article, s.product_name, s.product_barcode, s.barcode
        FROM scans s
        JOIN employees e ON e.id = s.employee_id
        WHERE s.scanned_at >= ? AND s.scanned_at < ?
        ORDER BY s.scanned_at DESC
        LIMIT 30
        """,
        (date_from, date_to),
    )

    top_rows = query_all(
        """
        SELECT article, product_name, COUNT(*)
        FROM scans
        WHERE scanned_at >= ? AND scanned_at < ?
          AND article IS NOT NULL AND article <> ''
        GROUP BY article, product_name
        ORDER BY COUNT(*) DESC, article
        LIMIT 8
        """,
        (date_from, date_to),
    )

    return jsonify(
        orders=int(totals[0] or 0),
        workers=int(totals[1] or 0),
        articles=int(totals[2] or 0),
        recent=[
            {
                "scanned_at": r[0],
                "employee": r[1],
                "article": r[2],
                "product_name": r[3],
                "product_barcode": r[4],
                "wb_barcode": r[5],
            }
            for r in recent_rows
        ],
        top_articles=[
            {"article": r[0], "product_name": r[1], "count": int(r[2] or 0)}
            for r in top_rows
        ],
    )


@wms_bp.get("/api/fbs/dashboard")
def dashboard():
    ensure_db()
    if not manager_access():
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
