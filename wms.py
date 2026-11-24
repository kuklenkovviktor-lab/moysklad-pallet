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
        """CREATE TABLE IF NOT EXISTS fbs_scan_requests (
            request_id TEXT PRIMARY KEY, scan_id TEXT NOT NULL,
            shift_id TEXT NOT NULL, employee_id TEXT NOT NULL,
            barcode TEXT NOT NULL, product_barcode TEXT NOT NULL
        )""",
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
        CREATE TABLE IF NOT EXISTS pdf_fbs_orders (
            wb_barcode TEXT PRIMARY KEY,
            article TEXT NOT NULL,
            product_barcode TEXT NOT NULL,
            wb_article TEXT,
            source_sha256 TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS pdf_fbs_shift_modes (
            shift_id TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fbs_pending_products (
            shift_id TEXT PRIMARY KEY,
            employee_id TEXT NOT NULL,
            product_barcode TEXT NOT NULL,
            article TEXT,
            product_name TEXT,
            started_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS fbs_skip_tokens (
            token_hash TEXT PRIMARY KEY,
            issued_by TEXT NOT NULL,
            issued_at TEXT NOT NULL,
            expires_at REAL NOT NULL,
            consumed_at TEXT,
            consumed_shift_id TEXT,
            consumed_employee_id TEXT,
            consumed_product_barcode TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS replenishment_tasks (
            id TEXT PRIMARY KEY,
            article TEXT NOT NULL,
            line_code TEXT NOT NULL,
            planned_qty INTEGER,
            completed_qty INTEGER,
            priority TEXT NOT NULL DEFAULT 'normal',
            note TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            created_by TEXT NOT NULL,
            assigned_employee_id TEXT,
            FOREIGN KEY(assigned_employee_id) REFERENCES employees(id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS replenishment_locations (
            id TEXT PRIMARY KEY,
            label TEXT NOT NULL UNIQUE,
            barcode TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS replenishment_task_events (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            actor_id TEXT,
            actor TEXT NOT NULL,
            kind TEXT NOT NULL,
            note TEXT,
            quantity INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(task_id) REFERENCES replenishment_tasks(id)
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
        """
        CREATE TABLE IF NOT EXISTS shift_admin_actions (
            id TEXT PRIMARY KEY,
            shift_id TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            admin_name TEXT NOT NULL,
            actual_ended_at TEXT NOT NULL,
            reason TEXT NOT NULL,
            recorded_at TEXT NOT NULL
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

        # Additional verification metadata does not alter earlier replenishment tasks.
        if DATABASE_URL:
            cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'replenishment_tasks'")
            task_cols = {r[0] for r in cur.fetchall()}
        else:
            cur.execute("PRAGMA table_info(replenishment_tasks)")
            task_cols = {r[1] for r in cur.fetchall()}
        for name, ddl in (
            ("product_id", "TEXT"),
            ("product_name", "TEXT"),
            ("verify_required", "INTEGER NOT NULL DEFAULT 0"),
            ("verified_product_barcode", "TEXT"),
            ("verified_product_at", "TEXT"),
            ("verified_location_barcode", "TEXT"),
            ("verified_location_at", "TEXT"),
        ):
            if name not in task_cols:
                cur.execute(f"ALTER TABLE replenishment_tasks ADD COLUMN {name} {ddl}")

        conn.commit()
        cur.close()

    seeds = [
        ("viktor", "Виктор", "manager", 1),
        ("anton", "Антон", "staff", 2),
        ("ilya", "Илья", "staff", 3),
        ("loader", "Грузчик", "staff", 4),
        ("denis", "Денис", "staff", 5),
    ]
    for emp_id, name, role, badge_number in seeds:
        badge_code = make_badge_code(emp_id)
        scanner_code = make_badge_scanner_code(emp_id)
        if query_one("SELECT id FROM employees WHERE id = ?", (emp_id,)):
            execute(
                "UPDATE employees SET "
                "badge_code = COALESCE(NULLIF(badge_code, ''), ?), "
                "scanner_code = COALESCE(NULLIF(scanner_code, ''), ?) "
                "WHERE id = ? AND "
                "(badge_code IS NULL OR badge_code = '' OR scanner_code IS NULL OR scanner_code = '')",
                (badge_code, scanner_code, emp_id),
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
        raise RuntimeError("Не удалось создать обязательную защиту от повторных WB-сканов. Проверьте дубли scans.barcode; данные автоматически не исправлялись.") from None
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
        "CREATE INDEX IF NOT EXISTS idx_pdf_fbs_product_barcode ON pdf_fbs_orders(product_barcode)",
        "CREATE INDEX IF NOT EXISTS idx_scans_scanned_at ON scans(scanned_at)",
        "CREATE INDEX IF NOT EXISTS idx_scans_employee_scanned_at ON scans(employee_id, scanned_at)",
        "CREATE INDEX IF NOT EXISTS idx_shifts_active_started ON shifts(ended_at, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_activities_shift_type_started ON activities(shift_id, activity_type, started_at)",
        "CREATE INDEX IF NOT EXISTS idx_operation_events_op_created ON operation_events(operation_type, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_operation_events_event_created ON operation_events(event_type, created_at)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_replenishment_one_active_per_cell ON replenishment_tasks(article, line_code) WHERE status IN ('new','started')",
        "CREATE INDEX IF NOT EXISTS idx_replenishment_status_priority ON replenishment_tasks(status, priority, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_replenishment_events_task_time ON replenishment_task_events(task_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_replenishment_locations_active ON replenishment_locations(active, label)",
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
    # The server, not a signed browser cookie, decides whether this terminal
    # is still authorized for the shift. Manager force-close revokes the shift.
    # Old cookies without a bound shift are upgraded only while a shift is open.
    if not session.get("manager_web"):
        bound_shift = str(session.get("employee_shift_id") or "")
        active_shift = query_one(
            "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL "
            "ORDER BY started_at DESC LIMIT 1", (employee_id,),
        )
        if bound_shift:
            if not active_shift or bound_shift != active_shift[0]:
                session.clear()
                return None
        elif active_shift:
            session["employee_shift_id"] = active_shift[0]
        else:
            # An unbound legacy session must be revoked after a shift closure.
            # A fresh synthetic fixture with no shift history is not a closed
            # shift; real employee login always creates the first shift.
            latest = query_one(
                "SELECT ended_at FROM shifts WHERE employee_id = ? "
                "ORDER BY started_at DESC LIMIT 1", (employee_id,),
            )
            if latest and latest[0] is not None:
                session.clear()
                return None
    # Upgrade legacy browser-only sessions on first valid request as well as
    # newly scanned sessions. A page reload/phone restart must retain the badge.
    if not session.permanent:
        session.permanent = True
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
    from fbs_transactions import event
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if not DATABASE_URL:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute(sql("SELECT id FROM employees WHERE id=?" + (" FOR NO KEY UPDATE" if DATABASE_URL else "")), (employee_id,))
            if not cur.fetchone():
                raise ValueError("Сотрудник не найден")
            cur.execute(sql("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"), (employee_id,))
            existing = cur.fetchone()
            if existing:
                return existing[0], False
            shift_id, ts = str(uuid.uuid4()), now_iso()
            cur.execute(sql("INSERT INTO shifts (id,employee_id,started_at) VALUES (?,?,?)"), (shift_id, employee_id, ts))
            cur.execute(sql("INSERT INTO activities (id,shift_id,employee_id,activity_type,started_at) VALUES (?,?,?,?,?)"), (str(uuid.uuid4()), shift_id, employee_id, initial_activity, ts))
            event(cur, employee_id, shift_id, "shift", "start", result=initial_activity)
            conn.commit()
            return shift_id, True
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def switch_activity(shift_id, employee_id, activity_type):
    from fbs_transactions import shift_transaction, active_worker, event
    with shift_transaction(shift_id) as (cur, shift):
        active_worker(cur, shift, shift_id, employee_id, picking=False)
        cur.execute(sql("SELECT activity_type FROM activities WHERE shift_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1"), (shift_id,))
        current = cur.fetchone()
        if current and current[0] == activity_type:
            return False
        ts = now_iso()
        cur.execute(sql("UPDATE activities SET ended_at=? WHERE shift_id=? AND ended_at IS NULL"), (ts, shift_id))
        cur.execute(sql("INSERT INTO activities (id,shift_id,employee_id,activity_type,started_at) VALUES (?,?,?,?,?)"), (str(uuid.uuid4()), shift_id, employee_id, activity_type, ts))
        event(cur, employee_id, shift_id, activity_type, "switch", result="active")
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


def activity_seconds_by_type(shift_id, now=None):
    """Sum all activity intervals by type, including an active interval."""
    now = now or datetime.now(timezone.utc)
    rows = query_all(
        """
        SELECT activity_type, started_at, ended_at
        FROM activities
        WHERE shift_id = ?
        """,
        (shift_id,),
    )
    durations = {}
    for activity_type, started_at, ended_at in rows:
        start = parse_iso(started_at)
        end = parse_iso(ended_at) if ended_at else now
        if start and end and end >= start:
            durations[activity_type] = durations.get(activity_type, 0.0) + (end - start).total_seconds()
    return {key: max(0, int(seconds)) for key, seconds in durations.items()}


def picking_seconds(shift_id, now=None):
    return activity_seconds_by_type(shift_id, now).get("picking", 0)


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
    activity_durations = activity_seconds_by_type(shift_id, parse_iso(shift[4]) if shift[4] else None)
    pick_sec = activity_durations.get("picking", 0)
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
        "replenishment_seconds": activity_durations.get("replenishment", 0),
        "activity_seconds": activity_durations,
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
<title>GRIT WMS · Вход</title>
<script src="/static/jsQR.js"></script>
<script src="/static/JsBarcode.all.min.js"></script>
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

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

body .card,body .barcode-zone,body .scanbox{background:#142337!important}
body .barcode-wrap,body canvas.barcode,body svg.barcode{background:#fff!important;color:#111!important}
@media(orientation:landscape) and (max-height:600px){html,body{background:#09121e!important}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head>
<body><div class="wrap"><div class="card">
<div class="header">
  <div class="header-left">
    <img src="/grit-logo.png?v=2" width="30" height="30" alt="GRIT WMS" style="border-radius:7px;object-fit:cover;flex:none">
    <div class="header-copy"><h1>GRIT WMS</h1><div class="muted">Терминал сотрудника</div></div>
  </div>
  <div id="scannerStatus" class="status">Zebra не подключена</div>
</div>
<div class="main">
  <div class="left">
    <div class="subtitle"><b>Вход через Zebra</b><br>1. Сканируй нижний код подключения<br>2. Сканируй бейдж сотрудника</div>
    <input id="hidBadge" class="scan-input" type="text" autocomplete="off" autocapitalize="none" spellcheck="false">
  </div>
  <div class="camera-box">
    <div class="camera-title"><span><b>Вход через камеру</b> · QR посещаемости / бейджа</span><button onclick="toggleCamera()">📷 Камера</button></div>
    <div id="reader"></div>
    <div id="result"></div>
  </div>
</div>
<div class="barcode-zone">
  <div class="barcode-wrap"><svg id="zebraBarcode"></svg></div>
</div>
</div></div>
<script>
// After a successful QR exit, forget terminal-local employee state.
// The server-side session has already been cleared.
if(new URLSearchParams(location.search).get('attendance_out')==='1'){
  try{
    ['onix_employee_id','fbs_shift_id','receipt_id','receipt_name'].forEach(k=>localStorage.removeItem(k));
    sessionStorage.removeItem('openExitQr');
  }catch(_){}
  history.replaceState(null,'','/login');
}
let scanner=null,busy=false,hidBuffer='',hidTimer=null;
const hidBadge=document.getElementById('hidBadge');
const ZEBRA_SERVICE_CODE='ONIX-ZEBRA-READY';
// Attendance QR is an action URL, NOT an employee badge.
// Keep the selected action until a real badge is scanned.
let pendingAttendanceAction=null;
function attendanceActionFromQr(value){
 const raw=String(value||'').trim();
 if(!raw)return null;
 try{
  const parsed=new URL(raw,location.origin);
  if(parsed.origin!==location.origin||parsed.username||parsed.password||parsed.search||parsed.hash)return null;
  return (parsed.pathname==='/attendance/in'||parsed.pathname==='/attendance/out')
    ? parsed.pathname : null;
 }catch(_){return null}
}
function setAttendanceAction(action){
 if(action!=='/attendance/in'&&action!=='/attendance/out')return false;
 pendingAttendanceAction=action;
 document.getElementById('result').innerHTML='<div class="ok">QR '+(action.endsWith('/in')?'ВХОД':'ВЫХОД')+' считан. Теперь сканируй личный бейдж.</div>';
 return true;
}

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
 // iOS HID scanners inherit the Russian physical-keyboard layout.
 // Only the exact service handshake is mapped: product / badge codes stay untouched.
 if(code==='ЩТШЧ-ЯУИКФ-КУФВН')return 'ONIX-ZEBRA-READY';
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
  if(setAttendanceAction(attendanceActionFromQr(code)))return;
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
 code=normalizeHidCode(code); if(!code||busy)return;
 if(setAttendanceAction(attendanceActionFromQr(code)))return;
 busy=true;
 const controller=new AbortController();
 const timer=setTimeout(()=>controller.abort(),8000);
 try{
  document.getElementById('result').innerHTML='<div class="ok">Бейдж считан · выполняю вход…</div>';
  const r=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({badge_code:code}),signal:controller.signal});
  const d=await r.json();
  if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Бейдж не принят')+'</div>';return}
  // Browser storage can be unavailable (private mode or blocked storage).
  // A successful server login must still navigate to the attendance action.
  try{
    const previousEmployee=localStorage.getItem('onix_employee_id');
    if(previousEmployee && previousEmployee!==d.employee.id){
      localStorage.removeItem('receipt_id');
      localStorage.removeItem('receipt_name');
    }
    localStorage.setItem('onix_employee_id',d.employee.id);
    localStorage.setItem('fbs_shift_id',d.shift_id||'');
  }catch(storageError){console.warn('Terminal cache unavailable');}
  document.getElementById('result').innerHTML='<div class="ok">Вход: '+String(d.employee.name)+'</div>';
  // Only our two attendance QR destinations are accepted, never arbitrary URLs.
  const requestedNext=new URLSearchParams(location.search).get('next');
  const nextTarget=pendingAttendanceAction ||
    ((requestedNext==='/attendance/in'||requestedNext==='/attendance/out')
      ? requestedNext : '/fbs');
  location.replace(nextTarget);
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
   if(setAttendanceAction(attendanceActionFromQr(code)))return;
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
async function stopBadgeCamera(){
 const active=scanner;
 scanner=null;
 if(!active)return;
 active.stopped=true;
 try{clearTimeout(active.timer)}catch(e){}
 try{active.stream.getTracks().forEach(t=>t.stop())}catch(e){}
 document.getElementById('reader').innerHTML='';
}

async function toggleCamera(){
 await enterTerminalFullscreen();
 const result=document.getElementById('result');
 const reader=document.getElementById('reader');

 if(scanner){
  await stopBadgeCamera();
  result.innerHTML='';
  setTimeout(focusHid,100);
  return;
 }

 if(typeof jsQR!=='function'){
  result.innerHTML='<div class="err">QR-модуль GRIT не загрузился. Обнови страницу.</div>';
  return;
 }
 if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
  result.innerHTML='<div class="err">Браузер не даёт доступ к камере.</div>';
  return;
 }

 result.innerHTML='<div class="ok">Камера включается…</div>';

 try{
  const stream=await navigator.mediaDevices.getUserMedia({
   audio:false,
   video:{
    facingMode:{ideal:'environment'},
    width:{ideal:1280},
    height:{ideal:720}
   }
  });

  const track=stream.getVideoTracks()[0];
  try{
   const caps=track.getCapabilities?track.getCapabilities():{};
   if(Array.isArray(caps.focusMode)&&caps.focusMode.includes('continuous')){
    await track.applyConstraints({advanced:[{focusMode:'continuous'}]});
   }
  }catch(e){}

  reader.innerHTML='';
  const video=document.createElement('video');
  video.setAttribute('playsinline','');
  video.setAttribute('autoplay','');
  video.muted=true;
  video.srcObject=stream;
  reader.appendChild(video);

  const canvas=document.createElement('canvas');
  canvas.style.display='none';
  reader.appendChild(canvas);
  const ctx=canvas.getContext('2d',{willReadFrequently:true});

  await video.play();

  const state={stream,video,canvas,ctx,timer:null,stopped:false};
  scanner=state;
  result.innerHTML='<div class="ok">Камера включена · наведи на QR бейджа</div>';

  const scanFrame=async()=>{
   if(scanner!==state||state.stopped)return;
   try{
    if(!busy&&video.readyState>=2&&video.videoWidth>0&&video.videoHeight>0){
     const maxWidth=900;
     const scale=Math.min(1,maxWidth/video.videoWidth);
     const w=Math.max(1,Math.round(video.videoWidth*scale));
     const h=Math.max(1,Math.round(video.videoHeight*scale));
     if(canvas.width!==w)canvas.width=w;
     if(canvas.height!==h)canvas.height=h;
     ctx.drawImage(video,0,0,w,h);
     const frame=ctx.getImageData(0,0,w,h);
     const found=jsQR(frame.data,w,h,{inversionAttempts:'dontInvert'});
     const code=normalizeHidCode(found&&found.data);
     if(code){
      if(!setAttendanceAction(attendanceActionFromQr(code))){
       result.innerHTML='<div class="ok">Бейдж считан · проверяю…</div>';
       await stopBadgeCamera();
       await login(code);
       return;
      }
      // Keep the camera open to scan the personal badge next.
     }
    }
   }catch(e){}
   if(scanner===state&&!state.stopped){
    state.timer=setTimeout(scanFrame,120);
   }
  };
  state.timer=setTimeout(scanFrame,80);
 }catch(e){
  await stopBadgeCamera();
  const msg=(e&&e.name==='NotAllowedError')
   ? 'Нет доступа к камере. Разреши камеру для GRIT в Safari.'
   : 'Камера не открылась: '+String((e&&e.message)||e||'ошибка');
  result.innerHTML='<div class="err">'+msg+'</div>';
  setTimeout(focusHid,100);
 }
}

window.addEventListener('pagehide',()=>{stopBadgeCamera()});
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
:root{--bg:#09121e;--panel:#1f3548;--panel2:#233d52;--text:#f2f7fc;--muted:#b9cbda;--line:#345067;--accent:#36546e;--accent2:#29465d}
*{box-sizing:border-box}
html,body{margin:0;width:100%;min-height:100%;background:var(--bg);color:var(--text)}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;-webkit-text-size-adjust:100%;min-height:100dvh}
.wrap{width:100%;max-width:1100px;margin:auto;padding:max(8px,env(safe-area-inset-top)) max(10px,env(safe-area-inset-right)) max(8px,env(safe-area-inset-bottom)) max(10px,env(safe-area-inset-left))}
.hidden{display:none!important}
.card{border-radius:9px;background:var(--panel)}
#topbar{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;min-height:22px;color:#d4e2ee;font-size:11px;font-weight:750}
#topbar .brand{letter-spacing:.02em}
#topbar .top-right{display:flex;flex-direction:column;align-items:flex-end;gap:2px;min-width:0}
#topbar .who{text-align:right;text-transform:uppercase}
#topbar .mini-earnings{white-space:nowrap;font-size:12px;font-weight:800;font-variant-numeric:tabular-nums;color:#e8f5ff}
#topbar .mini-earnings .mini-label{font-size:9px;font-weight:600;color:var(--muted);margin-right:4px}
#topbar .mini-earnings .mini-note{font-size:8px;font-weight:500;color:var(--muted);margin-left:2px}
#startCard{max-width:480px;margin:14vh auto 0;padding:20px}
#startCard h2{font-size:19px;margin:0 0 5px}
#startCard .muted{font-size:13px;color:var(--muted)}
button{font:inherit;cursor:pointer;border:0;border-radius:8px;background:var(--accent);color:#fff;padding:12px;font-weight:750}
button.secondary{background:var(--accent2);color:#eef5fa}
button.active{background:#436986;color:#fff;outline:1px solid #74b5dc}
#startCard button{width:100%;margin-top:14px}
#work{display:flex;flex-direction:column;gap:10px}
#scanCard{position:relative;display:flex;flex-direction:column;min-height:0}
.scan-stage{min-height:120px;height:clamp(120px,37dvh,260px);padding:12px 14px;border-radius:8px;background:var(--panel);display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:5px}
.scan-icon{width:36px;height:36px;color:#8bd3f0;flex:none}
.scan-stage .label{display:none}
#scanStage{font-size:clamp(19px,3.9vw,29px);font-weight:750;line-height:1.13;letter-spacing:-.015em}
#productInfo{font-size:clamp(11px,1.75vw,14px);color:var(--muted);line-height:1.3;max-width:95%;overflow-wrap:anywhere}
#reader{margin-top:7px;border-radius:8px;overflow:hidden;max-height:150px}
.manual-entry{position:absolute;left:4px;top:4px;pointer-events:none;height:0}
#barcode{position:absolute;left:0;top:0;width:1px;height:1px;opacity:0;border:0;padding:0;font-size:16px;outline:none}
#scanButton{display:none}
.manual-entry.open{position:static;height:auto;display:flex;gap:7px;pointer-events:auto;margin-top:8px}
.manual-entry.open #barcode{position:static;opacity:1;width:100%;height:40px;padding:9px;background:#eef4f8;color:#203242;border:1px solid #91afc2;border-radius:8px;min-width:0}
.manual-entry.open #scanButton{display:block;flex:none;min-width:118px;padding:8px;font-size:11px}

#result:empty{display:none}
#result{margin-top:6px;font-size:11px}
.ok,.err{border-radius:7px;padding:6px 9px;overflow-wrap:anywhere}
.ok{background:#1d463a;color:#b5ebcd}.err{background:#593033;color:#ffcbca}
#side{display:flex;flex-direction:column;gap:10px}
.stats-card{background:transparent}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.stat{min-width:0;border-radius:8px;background:var(--panel);padding:10px;text-align:center;min-height:70px;display:flex;flex-direction:column;justify-content:center;gap:2px}
.stat.orders{grid-column:auto}
.stat .small{font-size:11px;color:var(--muted)}
.stat .big{font-size:23px;font-weight:800;line-height:1.1;font-variant-numeric:tabular-nums}
.stat.time-meta,.goal,.progress,#remaining,#employeeName{display:none}
.activity-switch{background:transparent}
#activityToggle{width:100%;min-height:46px;background:#2b455b;font-size:14px;margin:0}
#activityMenu{margin-top:8px;padding:10px;background:var(--panel);border-radius:9px}
.activities{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.activities button{width:100%;min-height:40px;font-size:12px;padding:8px;margin:0}
#backToPicking{grid-column:1/-1;background:#345c52;color:#ebfff6}
.utility-grid{grid-column:1/-1;display:grid;grid-template-columns:1fr 1fr;gap:8px;border-top:1px solid var(--line);padding-top:9px;margin-top:2px}
.utility-grid button{background:#36556f}
.nav{grid-column:1/-1;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px;border-top:1px solid var(--line);padding-top:9px}
.nav a{display:flex;align-items:center;justify-content:center;min-height:35px;border-radius:7px;text-decoration:none;background:#2a455b;color:#dfeef8;text-align:center;font-size:11px;font-weight:650}
.nav a.active{background:#3d6581;color:#fff}
.exit-btn{grid-column:1/-1;display:block;text-align:center;background:#3b3643;color:#f7e1e1;border-radius:7px;text-decoration:none;padding:10px;font-size:12px}
.tsd-error{position:fixed;inset:0;z-index:5000;background:#b42318;color:#fff;display:none;align-items:center;justify-content:center;padding:20px}
.tsd-error.open{display:flex}
.tsd-error-box{width:min(720px,94vw);text-align:center}
.tsd-error-icon{font-size:58px;font-weight:900;line-height:1}
.tsd-error-title{font-size:34px;font-weight:950;margin-top:8px}
.tsd-error-text{font-size:18px;font-weight:750;margin-top:10px;line-height:1.3}
.tsd-error button{margin:18px auto 0;max-width:260px;background:#fff;color:#8a1f17;padding:12px}
@media (orientation:landscape) and (max-height:600px){
 .wrap{padding-top:max(4px,env(safe-area-inset-top));padding-bottom:max(4px,env(safe-area-inset-bottom))}
 #topbar{min-height:22px;margin-bottom:7px;font-size:10px}
 #work,#side{gap:7px}
 .scan-stage{height:clamp(115px,35dvh,180px);min-height:115px;padding:7px 10px;gap:2px}
 .scan-icon{width:28px;height:28px}
 #scanStage{font-size:clamp(19px,3.4vw,24px)}
 #productInfo{font-size:11px}
 .stat{min-height:61px;padding:7px}
 .stat .small{font-size:10px}.stat .big{font-size:20px}
 #activityToggle{min-height:43px}
 #activityMenu{max-height:calc(100dvh - 65px);overflow:auto}
}
@media (max-width:390px){
 .wrap{padding-left:7px;padding-right:7px}
 #scanStage{font-size:19px}
 .manual-entry.open{flex-wrap:wrap}
 .manual-entry.open #scanButton{width:100%}
}
/* GRIT worker UX: visible scan stages, safe-area touch targets and clear feedback.
   Presentation only: never change barcode capture or server transaction logic. */
:root{--bg:#09121e;--panel:#14283a;--panel2:#1b354b;--line:#35536b;--accent:#1a72b2;--accent2:#25445e}
#topbar{border-bottom:1px solid var(--line);padding-bottom:10px;min-height:36px}
#topbar .brand{color:#ff9c43;font-size:12px;letter-spacing:.065em}
#topbar .who{color:#e5f2fd}
.scan-stage{background:linear-gradient(145deg,#19354a,#112435);border:1px solid #35536b;border-left:4px solid #288fe0;transition:border-color .16s ease,background-color .16s ease}
#scanCard.awaiting-wb .scan-stage{border-left-color:#ff9c43;background:linear-gradient(145deg,#342c26,#16283b)}
#scanCard.awaiting-wb .scan-icon{color:#ffb56e}
#scanStage{color:#f5faff;text-wrap:balance}
#productInfo{color:#bfd2e1}
#productInfo b{color:#fff;overflow-wrap:anywhere}
#result{min-height:0;line-height:1.45}
#result .ok,#result .err{padding:11px 13px;font-size:13px;font-weight:650;border:1px solid transparent}
#result .ok{background:#173e35;color:#b6f2d3;border-color:#33765c}
#result .err{background:#49292d;color:#ffd2d0;border-color:#8f5259}
.stat{border:1px solid #324c63;background:#162e42}
.stat .big{color:#fff;font-variant-numeric:tabular-nums}
#activityToggle{border:1px solid #3e6480;background:#24435e;color:#f1f8ff}
#activityMenu{border:1px solid #35536b}
.activities button,.utility-grid button,.nav a{min-height:44px}
#backToPicking{border:1px solid #4c8c76}
.manual-entry.open #barcode{background:#101f2e;color:#f1f8ff;border:2px solid #5b91b8;min-height:44px}
.manual-entry.open #barcode::placeholder{color:#b7c8d8}
button:focus-visible,a:focus-visible,#barcode:focus-visible{outline:2px solid #ff9c43;outline-offset:2px}
@media (orientation:landscape) and (max-height:600px){
 #topbar{padding-bottom:6px;min-height:28px}
 .scan-stage{min-height:115px}
 #result .ok,#result .err{padding:7px 10px;font-size:12px}
}
@media (prefers-reduced-motion:reduce){.scan-stage{transition:none}}
</style>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head>
<body>
<div class="wrap">


<div id="topbar">
  <div class="brand">GRIT WMS · FBS</div>
  <div class="top-right">
    <div class="who" id="identity"></div>
    <div class="mini-earnings hidden" id="miniEarnings"><span class="mini-label">Выработка</span><span id="miniOrders">0</span> / <span id="miniEarned">0 ₽</span><span class="mini-note">расч.</span></div>
  </div>
</div>

<div class="card" id="startCard">
  <h2 id="startName">Сотрудник</h2>
  <div class="muted">Нажми кнопку и начинай работу.</div>
  <button onclick="startShift()">Начать смену</button>
</div>

<div id="work" class="hidden">
  <div class="card" id="scanCard">
    <div class="scan-stage" id="scanStageBox">
      <svg class="scan-icon" viewBox="0 0 36 36" fill="none" aria-hidden="true">
        <path d="M12 5H7a2 2 0 0 0-2 2v5M24 5h5a2 2 0 0 1 2 2v5M5 24v5a2 2 0 0 0 2 2h5M31 24v5a2 2 0 0 1-2 2h-5" stroke="currentColor" stroke-width="3" stroke-linecap="round"/>
        <path d="M12 18h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
      </svg>
      <div class="label">Сканируй сейчас</div>
      <div id="scanStage">СКАНИРУЙ ТОВАР</div>
      <div id="productInfo">Ожидание штрихкода</div>
    </div>
    <div id="reader" class="hidden"></div>
    <div class="manual-entry" id="manualEntry">
      <input id="barcode" type="text" autocomplete="off" autocapitalize="none" spellcheck="false" inputmode="none" placeholder="Сканируй ШК">
      <button class="secondary" id="scanButton" onclick="manualScan()">Принять скан</button>
    </div>
    <div id="result" role="status" aria-live="polite"></div>
    <div id="wbPreviousHint" role="status" aria-live="polite" style="font-size:12px;padding:6px 12px;color:#ffca83"></div>
    <div id="wbUnscannedHint" role="status" aria-live="polite" class="muted" style="font-size:12px;padding:6px 12px"></div>
  </div>
  <div id="side">
    <div class="card activity-switch">
      <button class="secondary" id="activityToggle" onclick="toggleActivityMenu()">☰ &nbsp; Другие операции</button>
      <div class="activities hidden" id="activityMenu">
        <button data-a="replenishment" onclick="activity('replenishment')">Пополнение</button>
        <button data-a="shipping" onclick="activity('shipping')">Отгрузка</button>
        <button data-a="pallets" onclick="activity('pallets')">Палеты</button>
        <button data-a="kgt" onclick="activity('kgt')">КГТ</button>
        <button data-a="downtime" onclick="activity('downtime')">Простой</button>
        <button data-a="other" onclick="activity('other')">Другое</button>
        <button id="backToPicking" class="hidden" onclick="activity('picking')">Вернуться к сборке</button>
        <div class="utility-grid">
          <button class="secondary" type="button" onclick="toggleManualEntry()">⌨ Ручной ввод</button>
          <button class="secondary" type="button" onclick="toggleCamera()">📷 Камера</button>
        </div>
        <div class="nav">
          <a href="/">📦 Приёмка</a>
          <a class="active" href="/fbs">🧺 Сборка</a>
          <a href="/move">↔ Перемещение</a>
          <a href="/replenishment">📦 Пополнение <span id="repCountFbs"></span></a>
          <a href="/fbs-diagnostics">📶 Тест скорости</a>
          <a href="/ozon-fbo">📦 Ozon FBO</a>
        </div>
        <a class="exit-btn" href="/attendance/exit-scan">🚪 Завершить смену</a>
      </div>
    </div>
  </div>
</div>
</div>
<script>
(function(){
 const n=document.getElementById('repCountFbs');
 async function update(){
  if(document.hidden)return;
  try{
   const r=await fetch('/api/replenishment/tasks',{cache:'no-store'});
   if(!r.ok)return;
   const d=await r.json();
   const tasks=d.tasks||[];
   const count=tasks.filter(t=>t.status==='new'||t.status==='started').length;
   n.textContent=count?'('+count+')':'';
  }catch(e){}
 }
 update();setInterval(update,30000);
})();
</script>
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
let fbsFocusInitialized=false,hidScanBuffer='',hidScanLastAt=0,hidScanResetTimer=null;

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
 document.getElementById('identity').textContent=d.employee.name;
 document.getElementById('startName').textContent=d.employee.name;
 if(d.employee.role==='manager')document.querySelectorAll('.manager-only').forEach(x=>x.classList.remove('hidden'));
 // The server owns the shift. Browser localStorage is only a convenience;
 // recover the correct shift even after Safari loses its local identifier.
 try{
  const stateResponse=await fetch('/api/shift/current',{cache:'no-store'});
  if(stateResponse.ok){
   const state=await stateResponse.json();
   if(state.active && state.stats && state.stats.employee_id===d.employee.id){
    shiftId=state.stats.shift_id;
    localStorage.setItem('fbs_shift_id',shiftId);
    // A new badge login starts with 'other'. Enter picking only in that case;
    // reopening /fbs must not silently overwrite receiving/replenishment time.
    if(state.stats.activity==='other'){
     const switched=await fetch('/api/fbs/activity',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:shiftId,activity_type:'picking'})});
     if(!switched.ok){const err=await switched.json();showErr(err.error||'Не удалось включить сборку');}
    }
    await refresh();
    return;
   }
   // Closed shift or no active shift: never reuse a stale browser shift ID.
   shiftId='';
   localStorage.removeItem('fbs_shift_id');
   return;
  }
 }catch(e){}
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
 // The first screen must be ready for HID scanning without a manual tap.
 if(!fbsFocusInitialized){fbsFocusInitialized=true;setTimeout(focusFbsInput,100)}
 document.getElementById('miniOrders').textContent=Number(d.orders||0).toLocaleString('ru-RU');
 document.getElementById('miniEarned').textContent=Number(d.earned||0).toLocaleString('ru-RU',{maximumFractionDigits:2})+' ₽';
 document.getElementById('miniEarnings').classList.remove('hidden');
 document.querySelectorAll('[data-a]').forEach(b=>b.classList.toggle('active',b.dataset.a===d.activity));
 const labels={picking:'☰  Другие операции',replenishment:'☰  Сейчас: Пополнение',shipping:'☰  Сейчас: Отгрузка',pallets:'☰  Сейчас: Палеты',kgt:'☰  Сейчас: КГТ',downtime:'☰  Сейчас: Простой',other:'☰  Сейчас: Другое'};
 const toggle=document.getElementById('activityToggle');
 if(toggle)toggle.textContent=labels[d.activity]||'Другая работа';
 const back=document.getElementById('backToPicking');
 if(back)back.classList.toggle('hidden',d.activity==='picking');
 if(queuedFbsScan())await retryFbsOutbox();
 await refreshPendingProduct();
}
function updateScanStage(){
 const input=document.getElementById('barcode');
 document.getElementById('scanCard').classList.toggle('awaiting-wb',!!pendingProduct);
 if(pendingProduct){
  document.getElementById('scanStage').textContent='СКАНИРУЙ ШК WB';
  document.getElementById('productInfo').innerHTML='<b>'+esc(pendingProduct.article||'Без артикула')+'</b> · '+esc(pendingProduct.name||'Товар')+'<br>ШК товара: '+esc(pendingProduct.barcode);
  input.placeholder='Теперь сканируй ШК WB';
 }else{
  document.getElementById('scanStage').textContent='СКАНИРУЙ ТОВАР';
  document.getElementById('productInfo').textContent='Ожидание штрихкода';
  input.placeholder='Сканируй ШК товара';
 }
}
async function refreshPendingProduct(){
 if(!shiftId)return;
 try{
  const r=await fetch('/api/fbs/pending?shift_id='+encodeURIComponent(shiftId),{cache:'no-store'});
  if(!r.ok)return;
  const d=await r.json();
  pendingProduct=d.pending?{barcode:d.pending.barcode,article:d.pending.article||'',name:d.pending.name||''}:null;
  updateScanStage();
 }catch(e){}
}
async function acceptSkipQr(code){
 const r=await fetch('/api/fbs/skip-item',{
  method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({shift_id:shiftId,code:code}),cache:'no-store'
 });
 const d=await r.json();
 if(!r.ok){showTsdError(d.error||'Пропуск товара не разрешён');return false}
 pendingProduct=null;updateScanStage();beep(true);
 document.getElementById('result').innerHTML=
  '<div class="ok">Пропуск товара разрешён руководителем. Заказ НЕ засчитан. Сканируй следующий товар.</div>';
 await refresh();return true;
}
let wbHintLastBarcode='',wbHintLastArticle='',wbHintRequestSerial=0,wbPreviousSerial=0;
async function refreshWbUnscannedHint(barcode, warnPrevious=false, scannedArticle=''){
 const container=document.getElementById(warnPrevious?'wbPreviousHint':'wbUnscannedHint');
 if(!container||!shiftId||!barcode)return;
 const serial=warnPrevious?++wbPreviousSerial:++wbHintRequestSerial;
 try{
  const url='/api/fbs/wb-unscanned-hints?'+new URLSearchParams({shift_id:shiftId,barcode});
  const r=await fetch(url,{cache:'no-store'});
  if(!r.ok)throw Error('status');
  const d=await r.json();
  if(serial!==(warnPrevious?wbPreviousSerial:wbHintRequestSerial))return;
  if(!d.available){container.textContent='';return}
  if(warnPrevious&&d.remaining===0){container.textContent='';return}
  // Product-info supplies the article even when today's queue is empty.
  // Never substitute a product EAN for the seller's article.
  const article=scannedArticle||d.article||'Артикул не указан в WB';
  const labels=(d.stickers||[]).map(x=>x.part_a+' / '+x.part_b).join(', ');
  container.textContent=(warnPrevious?'⚠ Осталось от предыдущего товара сегодня':'Сегодня · товар')+
   ': '+article+' · не отсканировано: '+d.remaining+(d.truncated?'+':'')+
   (labels?' · WB № '+labels:' · номера наклеек недоступны');
 }catch(e){if(serial===(warnPrevious?wbPreviousSerial:wbHintRequestSerial))container.textContent='Подсказка WB недоступна';}
}
async function acceptProduct(code){
 const r=await fetch('/api/fbs/product-info',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({barcode:code,shift_id:shiftId})});
 const d=await r.json();
 if(!r.ok){showTsdError(d.error||'Товар не найден');return false}
 const previous=wbHintLastBarcode;
 const previousArticle=wbHintLastArticle;
 if(previous&&previous!==d.barcode){
  // Warning is informational: never block the product-first WB scan flow.
  refreshWbUnscannedHint(previous,true,previousArticle);
 }else{document.getElementById('wbPreviousHint').textContent='';}
 wbHintLastBarcode=d.barcode;
 wbHintLastArticle=d.article||'';
 pendingProduct={barcode:d.barcode,article:d.article||'',name:d.name||''};
 updateScanStage();beep(true);
 // Refresh after the warning; newest product lookup wins over stale requests.
 setTimeout(()=>refreshWbUnscannedHint(d.barcode,false,d.article||''),0);
 document.getElementById('result').innerHTML='<div class="ok">✅ Товар подтверждён. Теперь сканируй ШК WB.</div>';
 return true;
}
const FBS_OUTBOX_KEY='grit_fbs_unconfirmed_v1';
let fbsRecovering=false;
function queuedFbsScan(){
 const raw=localStorage.getItem(FBS_OUTBOX_KEY);
 if(!raw)return null;
 try{return JSON.parse(raw)}catch(e){throw new Error('Повреждена ожидающая операция. Нужна проверка руководителя.')}
}
async function sendFbsScan(payload){
 const r=await fetch('/api/fbs/scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 const d=await r.json();
 if(!r.ok){if([400,404,409,422].includes(r.status))localStorage.removeItem(FBS_OUTBOX_KEY);if(d.reset_pending){pendingProduct=null;updateScanStage()}showTsdError(d.error||'Ошибка ШК WB');return false}
 localStorage.removeItem(FBS_OUTBOX_KEY);
 const verifiedBarcode=payload.product_barcode;
 pendingProduct=null;updateScanStage();beep(true);
 refreshWbUnscannedHint(verifiedBarcode,false,wbHintLastBarcode===verifiedBarcode?wbHintLastArticle:'');
 document.getElementById('result').textContent=d.replayed?'Заказ уже учтён ранее. Повтор не увеличил счётчик.':'✅ +1 заказ · '+payload.barcode;
 return true;
}
async function retryFbsOutbox(){
 const queued=queuedFbsScan();if(!queued||fbsRecovering)return false;
 if(queued.shift_id!==shiftId){showTsdError('Есть неподтверждённый скан другой смены. Нужна сверка руководителя.');return false}
 fbsRecovering=true;
 try{return await sendFbsScan(queued)}
 catch(e){showTsdError('Нет подтверждения сервера. Скан сохранён; повторим после восстановления связи.');return false}
 finally{fbsRecovering=false}
}
async function acceptWb(code){
 if(queuedFbsScan())return await retryFbsOutbox();
 const p=pendingProduct;if(!p)return false;
 if(code===p.barcode){
  showTsdError('❌ ДВА РАЗА ОТСКАНИРОВАН ТОВАР. Заказ не засчитан. Отсканируй стикер WB.');
  return false;
 }
 const payload={request_id:crypto.randomUUID(),shift_id:shiftId,barcode:code,
  product_barcode:p.barcode,article:p.article,product_name:p.name,scan_type:'normal'};
 // If durable storage is unavailable, fail BEFORE sending anything to the server.
 localStorage.setItem(FBS_OUTBOX_KEY,JSON.stringify(payload));
 const result=await retryFbsOutbox();
 if(result)await refresh();
 return result;
}
async function handleScan(code){
 code=(code||'').trim();if(!code||busy||!shiftId)return;
 busy=true;
 try{
  if(queuedFbsScan()){await retryFbsOutbox();await refresh();return}
  if(pendingProduct && /^97[0-9]{22}$/.test(code))await acceptSkipQr(code);
  else if(pendingProduct)await acceptWb(code);
  else await acceptProduct(code);
 }catch(e){showTsdError('Нет подтверждения сервера. Обнови связь; ожидающий товар сохранён.')}finally{
  setTimeout(()=>{busy=false;const el=document.getElementById('barcode');el.value='';el.focus()},60);
 }
}
// Android HID scanners act as hardware keyboards. Keep focus when possible, but
// also accept a complete scan if the operator has tapped elsewhere on the page.
function focusFbsInput(){
 if(!shiftId||document.hidden)return;
 const work=document.getElementById('work'),overlay=document.getElementById('tsdError'),input=document.getElementById('barcode');
 if(!work||work.classList.contains('hidden')||overlay.classList.contains('open')||!input)return;
 const active=document.activeElement;
 if(active&&active!==input&&['INPUT','TEXTAREA','SELECT'].includes(active.tagName))return;
 try{input.focus({preventScroll:true})}catch(e){input.focus()}
}
document.addEventListener('keydown',e=>{
 const input=document.getElementById('barcode'),work=document.getElementById('work');
 if(!shiftId||document.hidden||!work||work.classList.contains('hidden')||document.getElementById('tsdError').classList.contains('open'))return;
 // Existing focused-input handler processes the scan: never submit it twice.
 if(document.activeElement===input)return;
 const target=e.target;
 if(target&&(target.isContentEditable||['INPUT','TEXTAREA','SELECT'].includes(target.tagName)))return;
 if(e.ctrlKey||e.metaKey||e.altKey)return;
 if(e.key==='Enter'||e.key==='Tab'){
  if(hidScanBuffer.length>=4){
   e.preventDefault();
   const code=hidScanBuffer;
   hidScanBuffer='';
   clearTimeout(hidScanResetTimer);
   handleScan(code);
  }else{hidScanBuffer=''}
  return;
 }
 if(e.key.length!==1)return;
 const now=Date.now();
 if(now-hidScanLastAt>350)hidScanBuffer='';
 hidScanLastAt=now;
 if(hidScanBuffer.length<256)hidScanBuffer+=e.key;
 clearTimeout(hidScanResetTimer);
 hidScanResetTimer=setTimeout(()=>{hidScanBuffer=''},900);
});
window.addEventListener('pageshow',()=>setTimeout(focusFbsInput,120));
document.addEventListener('visibilitychange',()=>{if(!document.hidden)setTimeout(focusFbsInput,120)});
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
function toggleManualEntry(){
 const area=document.getElementById('manualEntry');
 const input=document.getElementById('barcode');
 const open=area.classList.toggle('open');
 input.setAttribute('inputmode',open?'text':'none');
 input.value='';
 document.getElementById('activityMenu').classList.add('hidden');
 if(open)setTimeout(()=>input.focus(),60);
 else setTimeout(focusFbsInput,60);
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
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Завершение смены</title>
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

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

#reader{background:#101f30}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap"><div class="card">
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
  // Clear only employee-specific local state after approved QR logout.
  // Normal browser close or network loss must never remove it.
  localStorage.removeItem('fbs_shift_id');
  localStorage.removeItem('receipt_id');
  localStorage.removeItem('receipt_name');
  localStorage.removeItem('onix_employee_id');
  localStorage.removeItem('onix_fbo_pending_scan_v1');
  localStorage.removeItem('onix_fbo_pending_undo_v1');
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
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · QR старшего</title>

<script src="https://cdn.jsdelivr.net/npm/jsbarcode@3.11.6/dist/JsBarcode.all.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:20px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.muted{color:#6b7785;font-size:13px}.top{display:flex;gap:8px;margin-bottom:12px}.top a,.top button{flex:1;padding:10px;border:0;border-radius:10px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}
@media print{.top{display:none}body{background:#fff}.wrap{padding:0}.card{border:0}}

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

#qr,#qr img,#qr canvas{background:#fff!important;padding:8px;border-radius:7px}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap">
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
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Терминал выхода</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:560px;margin:auto;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:20px}.muted{color:#6b7785;font-size:14px;line-height:1.45}.note{margin-top:14px;padding:12px;border-radius:11px;background:#eef3f6}.btn{width:100%;margin-top:14px;padding:14px;border:0;border-radius:11px;background:#48647a;color:#fff;font-size:16px;font-weight:750;cursor:pointer}.back{display:block;text-align:center;margin-top:12px;color:#536f83;text-decoration:none}.err{margin-top:12px;padding:10px;border-radius:10px;background:#f6e4e4;color:#8a4343}

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

#qr,#qr img,#qr canvas{background:#fff!important;padding:8px;border-radius:7px}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap"><div class="card">
<h1>📟 Терминал выхода</h1>
<div class="muted">Авторизуй это устройство один раз. После этого твоя учётка Виктора будет удалена из этой браузерной сессии, а устройство сможет показывать только динамический QR выхода.</div>
<div class="note"><b>Важно:</b> это не завершает твою рабочую смену в WMS. Оно только убирает твою авторизацию с этого конкретного устройства.</div>
<input id="supervisorCode" autocomplete="off" placeholder="Сканируй QR старшего" style="width:100%;padding:13px;font-size:16px;border:1px solid #cbd9e2;border-radius:10px;margin-top:12px">
<button class="btn" onclick="activate()">Авторизовать это устройство</button>
<div id="result"></div>
<a class="back" href="/stats">← Назад в статистику</a>
</div></div>
<script>
async function activate(){
 const supervisorCode=document.getElementById('supervisorCode').value.trim();
 if(!supervisorCode){document.getElementById('result').innerHTML='<div class="err">Сначала сканируй QR старшего</div>';return}
 const r=await fetch('/api/supervisor-terminal/activate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({supervisor_code:supervisorCode})});
 const d=await r.json();
 if(!r.ok){document.getElementById('result').innerHTML='<div class="err">'+String(d.error||'Ошибка')+'</div>';return}
 localStorage.setItem('supervisor_terminal_token',d.terminal_token);
 localStorage.removeItem('fbs_shift_id');
 location.href='/supervisor-terminal';
}
</script></body></html>"""


SUPERVISOR_TERMINAL_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · QR выхода</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh;display:flex;align-items:center;justify-content:center}.wrap{width:100%;max-width:560px;padding:16px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:20px;padding:22px;text-align:center;box-shadow:0 2px 12px rgba(52,72,91,.08)}.muted{color:#6b7785;font-size:14px;line-height:1.4}.qr{display:flex;justify-content:center;margin:20px 0}.ttl{font-size:34px;font-weight:850}.err{margin-top:12px;padding:12px;border-radius:10px;background:#f6e4e4;color:#8a4343}.link{display:inline-block;margin-top:12px;color:#536f83;text-decoration:none}

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

.qr{display:flex;justify-content:center}
.qr canvas,.qr img{background:#fff!important;padding:8px;border-radius:7px}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap"><div class="card">
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
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Вход руководителя</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d;min-height:100dvh;display:flex;align-items:center;justify-content:center}
.wrap{width:100%;max-width:460px;padding:18px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:18px;padding:22px;box-shadow:0 3px 14px rgba(52,72,91,.08)}
h1{margin:0 0 8px;font-size:25px}.muted{color:#6b7785;font-size:14px;line-height:1.45}
input{width:100%;margin-top:16px;padding:14px;border:1px solid #cbd9e2;border-radius:10px;font-size:16px;background:#fff}
button{width:100%;margin-top:10px;padding:13px;border:0;border-radius:10px;background:#48647a;color:#fff;font-size:16px;font-weight:750;cursor:pointer}
.err{margin-top:10px;padding:10px;border-radius:10px;background:#f6e4e4;color:#8a4343}

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

body .wrap .card{background:linear-gradient(145deg,#16283b,#101c2a)!important}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap"><div class="card">
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
<meta name="theme-color" content="#09111c"><title>GRIT WMS · Панель управления</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100dvh}.sidebar{background:#f7f9fb;border-right:1px solid #d7e0e7;padding:18px 12px;position:sticky;top:0;height:100dvh}.brand{padding:4px 8px 16px;border-bottom:1px solid #e0e7ec;margin-bottom:12px}.brand b{display:block;font-size:18px}.brand span{font-size:11px;color:#6b7785}.side-nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:9px;min-height:44px;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:750;font-size:13px;background:#eef3f6;color:#314657;border:1px solid transparent}.side-nav a:hover{background:#e3ebf0}.side-nav a.active{background:#48647a;color:#fff}.side-nav a.logout{margin-top:12px;background:#f3ecec;color:#794545}
.content{min-width:0;padding:16px}.wrap{max-width:1500px;margin:auto}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(52,72,91,.07)}.head{margin-bottom:12px}.head h1{margin:0;font-size:28px}.small{font-size:12px;color:#6b7785}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}.kpi{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:15px}.kpi .v{font-size:31px;font-weight:850;margin-top:4px}.kpi .l{font-size:12px;color:#6b7785}
.progress-card{margin-bottom:12px}.progress-head{display:flex;align-items:center;justify-content:space-between;gap:14px}.progress-main{display:flex;align-items:baseline;gap:8px}.progress-main .done{font-size:32px;font-weight:850}.progress-main .plan{font-size:18px;color:#6b7785;font-weight:750}.progress{height:14px;background:#dce4ea;border-radius:999px;overflow:hidden;margin:12px 0 7px}.progress>div{height:100%;background:#607f95;width:0%;transition:width .2s}.plan-edit{display:flex;align-items:center;gap:7px}.plan-edit input{width:110px;padding:8px 9px;border:1px solid #cbd9e2;border-radius:9px;font-size:14px;background:#fff}.plan-edit button{border:0;border-radius:9px;background:#48647a;color:#fff;padding:9px 11px;font-weight:750;cursor:pointer}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:9px}.section-title b{font-size:16px}.section-link{font-size:12px;color:#536f83;text-decoration:none;font-weight:750}
.worker{display:grid;grid-template-columns:1.25fr .6fr .8fr .9fr .9fr;gap:8px;padding:11px 0;border-bottom:1px solid #dde5eb}.worker:last-child{border:0}.worker b{font-size:14px}.op{font-weight:750}
.receiving-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.mini{background:#eef3f6;border-radius:10px;padding:11px}.mini .n{font-size:24px;font-weight:850}.mini .t{font-size:11px;color:#6b7785;margin-top:2px}
.dev{display:grid;grid-template-columns:80px 110px 140px minmax(0,1fr);gap:10px;padding:10px 0;border-bottom:1px solid #e1e7eb;align-items:center;font-size:12px}.dev:last-child{border:0}.dev .why{font-weight:750}.danger{color:#944747}.warn{color:#916d2d}.empty{padding:16px 0;color:#6b7785;font-size:13px}
.wbbar{display:none;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;padding:10px 14px;background:#f7f9fb;border:1px solid #d7e0e7;border-radius:14px}.wbstate{display:flex;align-items:center;gap:9px;font-size:13px}.dot{width:10px;height:10px;border-radius:50%;background:#a7b2bb;flex:0 0 auto}.dot.ok{background:#4f8a64}.dot.warn{background:#c08a32}.dot.err{background:#ad5757}.wbbar button{border:0;border-radius:9px;background:#48647a;color:#fff;padding:8px 11px;font-weight:750;cursor:pointer}.wbbar button:disabled{opacity:.55;cursor:wait}
.modal{position:fixed;inset:0;background:rgba(20,30,40,.48);display:none;align-items:center;justify-content:center;padding:20px;z-index:1000}.modal.open{display:flex}.modal-card{width:min(430px,96vw);background:#fff;border-radius:18px;padding:20px;box-shadow:0 18px 60px rgba(0,0,0,.22);text-align:center}.modal-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.modal-head h2{margin:0;font-size:21px}.close-btn{border:0;background:#eef3f6;color:#25313d;border-radius:9px;width:38px;height:38px;font-size:22px;cursor:pointer}.qrbox{display:flex;justify-content:center;align-items:center;min-height:290px;margin:14px 0}.ttl{font-size:30px;font-weight:850}.qr-note{font-size:12px;color:#6b7785;margin-top:5px}
@media(max-width:1000px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid #d7e0e7}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a.logout{margin-top:0}.content{padding:12px}.kpis{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}.worker{grid-template-columns:1fr 1fr}.worker>div:first-child{grid-column:1/-1}.progress-head{align-items:flex-start;flex-direction:column}.dev{grid-template-columns:70px 95px 1fr}.dev .object{display:none}}

/* GRIT WMS - desktop command center theme. Scope: STATS_PAGE only. */
:root{color-scheme:dark;--grit-bg:#09111c;--grit-panel:#122031;--grit-line:#2b4156;--grit-text:#edf5ff;--grit-muted:#99aec3;--grit-blue:#238fe0;--grit-orange:#ff922e}
body{background:var(--grit-bg);color:var(--grit-text)}
.app{grid-template-columns:230px minmax(0,1fr)}.sidebar{background:linear-gradient(180deg,#0b1725,#0c1b2a);border-color:#263a50;padding:18px 12px}.brand{border-color:#283e52;padding-bottom:19px}.brand b{display:flex;align-items:center;gap:8px;font-size:20px}.brand img{width:34px;height:34px;object-fit:cover}.brand span,.small{color:var(--grit-muted)}
.side-nav{gap:3px}.side-nav a{background:transparent;color:#d3dfeb;border-color:transparent;border-radius:9px;font-weight:650;min-height:39px;padding:8px 11px}.side-nav a:hover{background:#213549}.side-nav a.active{background:linear-gradient(100deg,#126bbb,#17539a);border:1px solid #287fc5;color:#fff}.side-nav a.logout{background:#2e2029;color:#e8b8b8}
.content{padding:20px 24px}.wrap{max-width:1650px;display:grid;grid-template-columns:minmax(0,1.5fr) minmax(300px,1fr);gap:14px}
.card,.kpi,.mini{background:linear-gradient(145deg,#162639,#0f1c2a);border:1px solid var(--grit-line);border-radius:12px;box-shadow:none;color:var(--grit-text)}
.card{padding:17px 18px}.head{grid-column:1/-1;margin:0;padding:0 2px 15px;background:transparent;border:0;border-bottom:1px solid #294055;border-radius:0}.head h1{font-size:27px;letter-spacing:-.4px}.head .small{font-size:13px;margin-top:5px}
.kpis{grid-column:1/-1;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:0}.kpi{min-width:0;padding:17px 18px;border-left:2px solid #2c93e0}.kpi:last-child{border-left-color:var(--grit-orange)}.kpi .v{font-size:31px;color:#fff}.kpi .l{color:#a6bdd0;font-weight:700}
.progress-card{grid-column:2;grid-row:3;margin:0}.progress-head{flex-direction:column;align-items:stretch;gap:12px}.progress-main .done{font-size:36px}.progress-main .plan{font-size:14px;color:#a6bed3}.progress{height:12px;background:#28394a}.progress>div{background:linear-gradient(90deg,#238fe0,#ff922e)}.plan-edit{flex-wrap:wrap}.plan-edit input{background:#0b1724;color:#fff;border:1px solid #3d566c;flex:1;min-width:95px}.plan-edit button,.wbbar button{background:#156fae;color:#fff;border:1px solid #348fce}
.hourly-card{grid-column:1;grid-row:3;margin:0!important;min-width:0}.hourly-card #hourlyChart{height:195px!important}.section-title b{color:#f3f8fd}.section-link{color:#5bbaff}
.grid2{grid-column:1/-1;grid-template-columns:minmax(0,1.4fr) minmax(280px,1fr);gap:14px;margin:0}.worker{border-color:#304456}.worker b{color:#fff}.worker .small{color:#9db1c5}
.mini{background:#1b2d41}.mini .t{color:#9cb2c6}
#completedCard{grid-column:1/-1;margin:0!important}.deviations-card{grid-column:1/-1}.dev{border-color:#304456}.empty{color:#a5b8ca}.warn{color:#ffb152}.danger{color:#ff8484}
.wbbar{grid-column:1/-1;background:#15273a;color:#f0f7ff;border-color:#2d516b;margin:0}
.processes,.quick-actions{grid-column:1/-1}.process-grid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin-top:14px}
.process-link{display:flex;flex-direction:column;gap:10px;background:#1a2c40;color:#e6f2fc;border:1px solid #344c63;border-radius:9px;text-decoration:none;padding:14px;min-height:94px;min-width:0}.process-link:hover{background:#223d56;border-color:#379ce6}.process-link.important{background:#292018;border-color:#e28a34}.process-name{font-size:14px;font-weight:750}.process-sub{font-size:12px;color:#a4bbce}
.quick-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:12px}.quick-grid a{background:#193047;color:#edf6ff;border:1px solid #36556f;border-radius:9px;text-align:center;text-decoration:none;padding:12px;font-size:13px;font-weight:700}.quick-grid a:hover{background:#215278}
.modal-card{background:#162739;color:#fff;border:1px solid #38536a}.close-btn{background:#2d4156;color:#fff}
@media(max-width:1150px){.wrap{grid-template-columns:1fr}.hourly-card,.progress-card{grid-column:1;grid-row:auto}.grid2{grid-template-columns:1fr}.process-grid{grid-template-columns:repeat(3,1fr)}.kpis{grid-template-columns:repeat(2,1fr)}}
@media(max-width:760px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.content{padding:12px}.process-grid,.quick-grid{grid-template-columns:repeat(2,1fr)}.worker{grid-template-columns:repeat(2,minmax(0,1fr))}.worker>div:first-child{grid-column:1/-1}}


/* Final GRIT dashboard component design: desktop and mobile. */
:root{color-scheme:dark;--grit-panel:#101f2f;--grit-line:#34516a;--grit-muted:#a7bed1}
body{font-size:14px;line-height:1.45;font-weight:450;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.app{grid-template-columns:240px minmax(0,1fr)}.sidebar{padding:17px 12px}.brand{padding:0 8px 16px;margin-bottom:13px}.brand b{font-size:20px;letter-spacing:.15px}.brand span{display:block;font-size:12px;padding-top:4px}
.side-nav{gap:3px}.side-nav a{display:flex;gap:12px;padding:9px 11px;min-height:41px;font-weight:570;font-size:13.5px;letter-spacing:0;white-space:normal}.side-nav a.active{font-weight:710}.side-nav a.logout{margin-top:10px;color:#ff9c5a}
.ui-icon{display:inline-block;flex:0 0 20px;width:20px;height:20px;vertical-align:middle}.icon-defs{position:absolute;width:0;height:0;overflow:hidden;pointer-events:none}
.content{padding:20px 24px 30px}.wrap{gap:14px}.card,.kpi{border-radius:13px;background:linear-gradient(145deg,#142538 0%,#0e1a28 100%);border:1px solid #314c63;box-shadow:0 5px 16px rgba(0,0,0,.09)}
.card{padding:18px 19px}.head{background:transparent;border:0;border-bottom:1px solid #294258;border-radius:0;padding:0 0 14px}.head h1{font-size:clamp(23px,2vw,30px);font-weight:750;line-height:1.2;letter-spacing:-.65px}.head .small{margin-top:7px;font-size:13px;line-height:1.55}
.kpis{gap:12px}.kpi{min-height:102px;padding:18px;display:flex;flex-direction:column;justify-content:center;border-left:1px solid #34516a}.kpi:last-child{border-color:#a0602c}.kpi-heading{display:flex;align-items:center;gap:10px}.kpi-icon{width:25px;height:25px;flex:none;color:#56b5fa}.kpi:last-child .kpi-icon{color:#ffa044}.kpi .l{font-size:12px;color:#c1d4e4;font-weight:590;line-height:1.35}.kpi .v{font-size:34px;margin:6px 0 0 35px;font-weight:760;letter-spacing:-.8px}
.section-title{margin-bottom:15px;gap:14px}.section-title b{font-size:16px;font-weight:715}.title-with-icon{display:inline-flex;align-items:center;gap:10px}.title-with-icon .ui-icon{color:#69bafb}.deviations-card .title-with-icon .ui-icon{color:#ffac52}
.small{color:#a9bfd0;font-size:12px}.hourly-card,.progress-card{min-height:245px}.hourly-card .hourly-chart{height:187px!important;position:relative;display:flex;align-items:flex-end;gap:5px;padding-top:10px}.hour-cell{flex:1;min-width:0;height:100%;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;gap:5px}.hour-bar{width:100%;background:linear-gradient(180deg,#41a9f5,#176198);border-radius:4px 4px 1px 1px;min-height:2px}.hour-value{font-size:10px;font-weight:700;color:#dfebf9}.hour-label{font-size:9px;color:#96b0c5}.hourly-chart.is-empty{padding:0 0 20px}.empty-hours{position:absolute;inset:0 0 22px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:5px;text-align:center}.empty-hours .ui-icon{width:32px;height:32px;color:#6c879f;margin-bottom:7px}.empty-hours strong{font-size:15px;font-weight:650}.empty-hours span{font-size:12px;color:#9db2c5}
.hour-ticks{display:flex;justify-content:space-between;position:absolute;bottom:0;left:0;right:0;border-top:1px solid #405c73;color:#91adc3;font-size:9px;padding-top:5px}
.progress-card .progress-label{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px;color:#e8f3ff}.progress-label .ui-icon{color:#68baff}.progress-main .done{font-weight:800}.progress-card .plan-edit{margin-top:12px}.plan-edit input{min-height:40px;font-size:13px}.plan-edit button{min-height:40px;font-size:13px}.progress-card .progress{margin-top:15px}
.process-grid{gap:12px}.process-link{display:flex;justify-content:center;gap:10px;min-height:84px;border-radius:10px;padding:15px}.process-link.important{border-color:#ef9031;background:linear-gradient(140deg,#302319,#211c1a)}.process-link:hover{transform:translateY(-1px)}.process-name{display:flex;align-items:center;gap:10px;font-weight:680;font-size:14px}.process-name .ui-icon{color:#84b7e4;width:23px;height:23px}.process-link.important .process-name .ui-icon{color:#ff9c42}.process-sub{font-size:12px;margin-left:33px}
.grid2 .card{min-height:138px}.worker{gap:9px}.worker b{font-weight:700}.mini{border:1px solid #35516a}.mini .n{font-weight:750}
.empty-state{display:flex;align-items:center;justify-content:center;flex-direction:column;min-height:95px;text-align:center;gap:4px;color:#aec5d8}.empty-state strong{color:#e0edfa;font-size:14px;font-weight:650}.empty-state span{font-size:12px}.empty-icon{width:28px;height:28px;color:#607f9a;margin-bottom:5px}
.quick-grid a{min-height:50px;display:flex;justify-content:center;align-items:center;letter-spacing:.1px;font-weight:660}.quick-grid a:hover{background:#204b6a}
a:focus-visible,button:focus-visible,input:focus-visible{outline:2px solid #ff9b43;outline-offset:2px}.modal-card{background:#162538;color:#f1f6ff}.modal-card .small{color:#b1c7d8}
@media(max-width:1160px){.app{grid-template-columns:210px minmax(0,1fr)}.content{padding:16px}.wrap{grid-template-columns:1fr}.hourly-card,.progress-card{grid-column:1;grid-row:auto}.grid2{grid-template-columns:1fr}.side-nav a{font-size:12px;gap:8px}.kpis{grid-template-columns:repeat(2,1fr)}.process-grid{grid-template-columns:repeat(3,1fr)}}
@media(max-width:780px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a{min-height:40px}.content{padding:12px}.head h1{font-size:23px}.card{padding:15px}.process-grid,.quick-grid{grid-template-columns:repeat(2,1fr)}.kpi .v{font-size:27px}.kpi{min-height:95px}.grid2{grid-template-columns:1fr}.worker{grid-template-columns:repeat(2,1fr)}.worker>div:first-child{grid-column:1/-1}}
@media(max-width:480px){.process-grid{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.kpi{padding:12px}.kpi .v{margin-left:0}.kpi-heading{align-items:flex-start}.section-title{flex-wrap:wrap}.receiving-grid{grid-template-columns:repeat(3,minmax(0,1fr))}.mini{padding:8px}.mini .n{font-size:22px}}

.kpi-unpicked-link{display:flex;flex-direction:column;justify-content:center;color:inherit;text-decoration:none;cursor:pointer;transition:border-color .14s,background .14s}.kpi-unpicked-link:hover{border-color:#45a5ef;background:#1a3047}.kpi-unpicked-link:focus-visible{outline:2px solid #ff9230;outline-offset:3px}.kpi-unpicked-link .l span{color:#75c5ff;margin-left:4px}
/* GRIT branding: decorative warehouse artwork below logout, never covering controls. */
.sidebar{display:flex;flex-direction:column;overflow-y:auto;overflow-x:hidden;scrollbar-width:thin;scrollbar-color:#30445a transparent}
.sidebar .side-nav{flex:0 0 auto}
.sidebar-art{flex:0 0 230px;width:calc(100% + 24px);margin:14px -12px -17px;position:relative;pointer-events:none;background:#0b1726 url("/grit-warehouse-sidebar.svg?v=2") center center/cover no-repeat;border-top:1px solid #203549;min-height:230px}
.sidebar-art::before{content:"";position:absolute;inset:0;background:linear-gradient(180deg,#0b1726 0%,rgba(11,23,38,.05) 24%,rgba(11,23,38,0) 68%,rgba(11,23,38,.18) 100%);pointer-events:none}
@media(min-width:1161px){.sidebar{padding-top:13px}.sidebar .brand{padding-bottom:11px;margin-bottom:7px}.sidebar .side-nav{gap:1px}.sidebar .side-nav a{min-height:35px;padding:6px 9px;font-size:12.5px;gap:9px}.sidebar-art{margin-top:auto;flex-basis:230px}}
@media(max-width:1159px){.sidebar-art{flex-basis:220px;min-height:220px}}
@media(max-width:780px){.sidebar{overflow:visible}.sidebar-art{display:none}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body>
<svg xmlns="http://www.w3.org/2000/svg" class="icon-defs" aria-hidden="true"><defs><symbol id="ic-home" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z"/></symbol><symbol id="ic-calendar" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="16" rx="2"/><path d="M7 3v4M17 3v4M3 10h18M8 14h3M8 17h3"/></symbol><symbol id="ic-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 3h8l4 4v14H6zM14 3v5h5M9 12h6M9 16h6"/></symbol><symbol id="ic-clipboard" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4.5V3h6v1.5M9 11h6M9 15h6"/></symbol><symbol id="ic-barcode" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 5v14M6 6v12M10 5v14M13 6v12M17 5v14M20 6v12"/></symbol><symbol id="ic-download" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4"/></symbol><symbol id="ic-boxes" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 5 3v6l-5 3-5-3V6zM7 13l-5 3v5l5 2 5-2v-5zM17 13l-5 3v5l5 2 5-2v-5z"/></symbol><symbol id="ic-map" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3zM9 3v15M15 6v15"/></symbol><symbol id="ic-scan" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 9V5a1 1 0 0 1 1-1h4m6 0h4a1 1 0 0 1 1 1v4m0 6v4a1 1 0 0 1-1 1h-4m-6 0H5a1 1 0 0 1-1-1v-4M4 12h16"/></symbol><symbol id="ic-activity" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 18h3v-7H3zM10 18h3V6h-3zM17 18h3V3h-3z"/></symbol><symbol id="ic-package" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10"/></symbol><symbol id="ic-users" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6m1 4a5 5 0 0 1 4 5"/></symbol><symbol id="ic-link" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7 .2l3-3a5 5 0 1 0-7-7l-2 2m3 6a5 5 0 0 0-7-.2l-3 3a5 5 0 1 0 7 7l2-2"/></symbol><symbol id="ic-badge" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="16" height="17" rx="2"/><circle cx="12" cy="10" r="2"/><path d="M8 17c1-3 7-3 8 0M8 2h8"/></symbol><symbol id="ic-qr" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><path d="M14 14h3v3h-3zM21 14v3h-2M14 21h4m3-2v2"/></symbol><symbol id="ic-logout" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h4M14 7l5 5-5 5M19 12H9"/></symbol><symbol id="ic-target" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1.5"/><path d="m16 8 5-5"/></symbol><symbol id="ic-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 10 18H2zM12 9v5M12 17v.2"/></symbol><symbol id="ic-clock" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></symbol><symbol id="ic-check" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m7 12 3 3 7-7"/></symbol><symbol id="ic-layers" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3 9 5-9 5-9-5zM3 12l9 5 9-5M3 16l9 5 9-5"/></symbol><symbol id="ic-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="10" r="7"/><path d="m15 15 6 6"/></symbol><symbol id="ic-truck" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h11v11H3zM14 10h4l3 4v3h-7z"/><circle cx="7" cy="18" r="2"/><circle cx="17" cy="18" r="2"/></symbol></defs></svg>
<div class="app">
<aside class="sidebar">
 <div class="brand"><b><img src="/grit-logo.png?v=2" width="24" height="24" alt="" style="vertical-align:middle;border-radius:5px;margin-right:6px">GRIT WMS</b><span>Панель управления</span></div>
 <nav class="side-nav">
  <a class="active" href="/manager"><svg class="ui-icon" aria-hidden="true"><use href="#ic-home"></use></svg>Главная</a>
  <a href="/manager/shift-history"><svg class="ui-icon" aria-hidden="true"><use href="#ic-calendar"></use></svg>История смен</a>
  <a href="/manager/attendance"><svg class="ui-icon" aria-hidden="true"><use href="#ic-calendar"></use></svg>Посещаемость</a>
  <a href="/manager/fbs-pdf"><svg class="ui-icon" aria-hidden="true"><use href="#ic-file"></use></svg>Загрузка PDF FBS</a>
  <a href="/manager/fbs-skip"><svg class="ui-icon" aria-hidden="true"><use href="#ic-qr"></use></svg>Пропуск товара · QR</a>
  <a href="/fbs-journal"><svg class="ui-icon" aria-hidden="true"><use href="#ic-clipboard"></use></svg>Журнал FBS</a>
  <a href="/history"><svg class="ui-icon" aria-hidden="true"><use href="#ic-barcode"></use></svg>История ШК</a>
  <a href="/receiving-monitor"><svg class="ui-icon" aria-hidden="true"><use href="#ic-download"></use></svg>Приёмка</a>
   <a href="/manager/replenishment"><svg class="ui-icon" aria-hidden="true"><use href="#ic-boxes"></use></svg>Пополнение</a>
   <a href="/manager/warehouse-map"><svg class="ui-icon" aria-hidden="true"><use href="#ic-map"></use></svg>Карта склада</a>
   <a href="/manager/cell-audit"><svg class="ui-icon" aria-hidden="true"><use href="#ic-scan"></use></svg>Обход ячеек</a>
   <a href="/manager/fbs-diagnostics"><svg class="ui-icon" aria-hidden="true"><use href="#ic-activity"></use></svg>Скорость сканирования</a>
  <a href="/manager/ozon-fbo"><svg class="ui-icon" aria-hidden="true"><use href="#ic-package"></use></svg>Ozon FBO</a>
  <a href="/manager/partners"><svg class="ui-icon" aria-hidden="true"><use href="#ic-users"></use></svg>Партнёры</a>
  <a href="/manager/integrations"><svg class="ui-icon" aria-hidden="true"><use href="#ic-link"></use></svg>Интеграции</a>
  <a href="/badges"><svg class="ui-icon" aria-hidden="true"><use href="#ic-badge"></use></svg>Бейджи</a>
  <a href="/manager/attendance/qr"><svg class="ui-icon" aria-hidden="true"><use href="#ic-qr"></use></svg>QR посещаемости</a>
  <a class="logout" href="#" onclick="managerLogout();return false"><svg class="ui-icon" aria-hidden="true"><use href="#ic-logout"></use></svg>Выйти из ПК</a>
 </nav>
 <div class="sidebar-art" role="img" aria-label="Фирменная иллюстрация склада GRIT"></div>
</aside>

<main class="content"><div class="wrap">
 <div class="card head"><h1>Операционный центр склада</h1><div class="small">GRIT WMS · Управление складскими процессами в реальном времени · Обновление каждые 10 секунд</div></div>

 <div id="wbBar" class="wbbar">
  <div class="wbstate"><span id="wbDot" class="dot"></span><div><b id="wbTitle">WB-защита</b><div class="small" id="wbText"></div></div></div>
  <button id="wbSyncBtn" onclick="syncWb()">Обновить WB</button>
 </div>

 <div class="kpis">
  <div class="kpi"><div class="kpi-heading"><svg class="kpi-icon" aria-hidden="true"><use href="#ic-package"></use></svg><div class="l">WB FBS собрано сегодня</div></div><div class="v" id="todayOrders">0</div></div>
  <a class="kpi kpi-unpicked-link" href="/manager/wb-fbs/unpicked" aria-label="Открыть список несобранных заказов WB"><div class="kpi-heading"><svg class="kpi-icon" aria-hidden="true"><use href="#ic-target"></use></svg><div class="l">Осталось собрать <span aria-hidden="true">↗</span></div></div><div class="v" id="remaining">—</div></a>
  <div class="kpi"><div class="kpi-heading"><svg class="kpi-icon" aria-hidden="true"><use href="#ic-download"></use></svg><div class="l">Приёмка сегодня, шт.</div></div><div class="v" id="receivingUnits">0</div></div>
  <div class="kpi"><div class="kpi-heading"><svg class="kpi-icon" aria-hidden="true"><use href="#ic-alert"></use></svg><div class="l">Отклонения сегодня</div></div><div class="v" id="deviationCount">0</div></div>
 </div>

 <div class="card progress-card">
  <div class="progress-head">
   <div>
    <div class="small progress-label"><svg class="ui-icon" aria-hidden="true"><use href="#ic-target"></use></svg> Прогресс WB FBS</div>
    <div class="progress-main"><span class="done" id="progressDone">0</span><span class="plan" id="progressPlan"> / ожидаем данные WB</span></div>
   </div>
  </div>
  <div class="progress"><div id="progressBar"></div></div>
  <div class="small" id="progressText">Задай план на день, чтобы видеть остаток и процент выполнения.</div>
  <div class="small" id="wbAssemblyInfo" style="margin-top:9px">WB · На сборке: источник не подключён</div>
  <div class="small" style="margin-top:6px"><a href="/manager/integrations/wildberries/history">История WB FBS · сборка и отгрузка →</a></div>
  <div id="wbDailyFlow" class="small" style="margin-top:7px">История: загрузка…</div>
 </div>

 <div class="card hourly-card">
  <div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-layers"></use></svg>Сборка FBS по часам</b><span class="small">собрано заказов за каждый час</span></div>
  <div id="hourlyChart" class="hourly-chart" aria-label="Количество заказов FBS по часам"></div>
 </div>

 <section class="card processes" aria-label="Ключевые складские процессы">
 <div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-layers"></use></svg>Ключевые складские процессы</b><span class="small">Рабочие модули</span></div>
 <div class="process-grid">
 <a class="process-link" href="/receiving-monitor"><span class="process-name"><svg class="ui-icon" aria-hidden="true"><use href="#ic-download"></use></svg>Приёмка</span><span class="process-sub">Поставки и операции</span></a>
 <a class="process-link" href="/manager/replenishment"><span class="process-name"><svg class="ui-icon" aria-hidden="true"><use href="#ic-boxes"></use></svg>Пополнение</span><span class="process-sub">Задания по ячейкам</span></a>
 <a class="process-link important" href="/fbs-journal"><span class="process-name"><svg class="ui-icon" aria-hidden="true"><use href="#ic-package"></use></svg>Сборка FBS</span><span class="process-sub">Заказы и сканы</span></a>
 <a class="process-link" href="/manager/warehouse-map"><span class="process-name"><svg class="ui-icon" aria-hidden="true"><use href="#ic-map"></use></svg>Карта склада</span><span class="process-sub">Адресное хранение</span></a>
 <a class="process-link" href="/manager/shift-history"><span class="process-name"><svg class="ui-icon" aria-hidden="true"><use href="#ic-calendar"></use></svg>История смен</span><span class="process-sub">Выработка и смены</span></a>
 </div></section>
<div class="grid2">
  <div class="card">
   <div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-users"></use></svg>Сейчас работают</b><span class="small"><span id="active">0</span> активных · <a class="section-link" href="/manager/shifts">Управление сменами →</a></span></div>
   <div id="workers" aria-live="polite"><div class="empty-state"><svg class="empty-icon" aria-hidden="true"><use href="#ic-users"></use></svg><strong>Загрузка сотрудников</strong><span>Проверяем активные смены</span></div></div>
  </div>

  <div class="card">
   <div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-download"></use></svg>Приёмка сегодня</b><a class="section-link" href="/receiving-monitor">Открыть приёмку →</a></div>
   <div class="receiving-grid">
    <div class="mini"><div class="n" id="receivingOps">0</div><div class="t">операций</div></div>
    <div class="mini"><div class="n" id="receivingWorkers">0</div><div class="t">сотрудников</div></div>
    <div class="mini"><div class="n" id="receivingCompleted">0</div><div class="t">документов завершено</div></div>
   </div>
  </div>
 </div>

 <div class="card" id="completedCard" style="margin-bottom:12px;display:none">
  <div class="section-title"><b>Завершённые смены сегодня</b><span class="small">время по операциям сохраняется после выхода</span></div>
  <div id="completedWorkers"></div>
 </div>

 <div class="card deviations-card">
  <div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-alert"></use></svg>Последние ошибки и отклонения</b><span class="small">только то, что требует внимания</span></div>
  <div id="deviations" aria-live="polite"><div class="empty-state"><strong>Загрузка событий</strong></div></div>
 </div>
<section class="card quick-actions"><div class="section-title"><b class="title-with-icon"><svg class="ui-icon" aria-hidden="true"><use href="#ic-activity"></use></svg>Быстрые действия</b><span class="small">Действующие разделы</span></div><div class="quick-grid"><a href="/fbs-journal">Журнал FBS</a><a href="/manager/replenishment">Пополнение</a><a href="/receiving-monitor">Приёмка</a><a href="/manager/shift-history">Отчёт по сменам</a></div></section>
</div></main>
</div>


<script>
let wbAssemblyCount=null;
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return ts}}
function dayKey(){return new Date(Date.now()+3*3600000).toISOString().slice(0,10)}
function dayBounds(){
 const start=new Date(dayKey()+'T00:00:00+03:00');
 const end=new Date(start.getTime()+86400000);
 return {from:start.toISOString(),to:end.toISOString()};
}
function activityName(v){return ({picking:'Сборка',replenishment:'Пополнение',receiving:'Приёмка',shipping:'Отгрузка',pallets:'Палеты',kgt:'КГТ',downtime:'Простой',other:'Другое'})[v]||v||'—'}
function activityMinutes(ts){if(!ts)return 0;return Math.max(0,Math.round((Date.now()-new Date(ts).getTime())/60000))}
function formatDuration(seconds){
 const m=Math.max(0,Math.round(Number(seconds||0)/60));
 return m>=60?Math.floor(m/60)+' ч '+m%60+' мин':m+' мин';
}
function renderEmployeeShift(x,completed){
 const current=completed?'Завершена · '+fmt(x.ended_at):activityName(x.activity)+' · '+activityMinutes(x.activity_started_at)+' мин';
 const extra=Object.entries(x.activity_seconds||{}).filter(([type,sec])=>type!=='picking'&&type!=='replenishment'&&sec>=60)
   .map(([type,sec])=>activityName(type)+': '+formatDuration(sec)).join(' · ');
 return '<div class="worker"><div><b>'+esc(x.employee)+'</b><div class="small op">'+esc(current)+'</div>'
   +(extra?'<div class="small">'+esc(extra)+'</div>':'')+'</div>'
   +'<div><b>'+Number(x.wb_fbs_orders??x.orders??0)+'</b><div class="small">WB FBS, заказов</div>'
   +'<div class="small" style="margin-top:4px">Ozon FBO: <b>'+Number(x.ozon_fbo_units||0)+'</b> шт. · '+Number(x.ozon_fbo_scans||0)+' скан.</div></div>'
   +'<div><b>'+Number(x.orders_per_hour||0)+'</b><div class="small">заказ/ч</div></div>'
   +'<div><b>'+formatDuration(x.picking_seconds)+'</b><div class="small">чистая сборка</div></div>'
   +'<div><b>'+formatDuration(x.replenishment_seconds)+'</b><div class="small">пополнение за смену</div></div></div>';
}
function deviationLabel(v){return ({product_mismatch:'Не тот товар',wb_order_missing:'Заказ WB не найден',duplicate_wb:'Повторный ШК WB',wb_same_as_product:'Товарный ШК вместо WB',product_not_found:'Неизвестный товар',logout_denied:'Неверный QR выхода',scan_rejected:'Скан отклонён'})[v]||v}
function deviationClass(v){return ['product_mismatch','duplicate_wb','wb_same_as_product','product_not_found'].includes(v)?'danger':'warn'}
function renderHourly(rows){
 const hours=Array.from({length:24},(_,h)=>({h,n:0}));
 rows.forEach(x=>{const d=new Date(x.scanned_at);if(!isNaN(d))hours[d.getHours()].n++});
 const max=Math.max(1,...hours.map(x=>x.n)),total=hours.reduce((n,x)=>n+x.n,0),box=document.getElementById('hourlyChart');
 if(!total){
  box.classList.add('is-empty');
  box.innerHTML='<div class="empty-hours"><svg class="ui-icon" aria-hidden="true"><use href="#ic-activity"></use></svg><strong>Нет данных за сегодня</strong><span>Сборка заказов ещё не началась</span></div><div class="hour-ticks"><span>00</span><span>04</span><span>08</span><span>12</span><span>16</span><span>20</span><span>23</span></div>';
  return;
 }
 box.classList.remove('is-empty');
 box.innerHTML=hours.map(x=>{
  const pct=Math.max(x.n?5:1,Math.round(x.n/max*100));
  return '<div class="hour-cell" title="'+String(x.h).padStart(2,'0')+':00 · '+x.n+'"><div class="hour-value">'+(x.n||'')+'</div><div class="hour-bar" style="height:'+pct+'%"></div><div class="hour-label">'+String(x.h).padStart(2,'0')+'</div></div>';
 }).join('');
}

function renderProgress(done){
 const n=Math.max(0,Number(done)||0);
 document.getElementById('progressDone').textContent=n;
 if(wbAssemblyCount===null){
  document.getElementById('remaining').textContent='—';
  document.getElementById('progressPlan').textContent=' / очередь WB недоступна';
  document.getElementById('progressBar').style.width='0%';
  document.getElementById('progressText').textContent='Собрано сегодня: '+n+' · актуальный остаток WB неизвестен';
  return;
 }
 const rest=Math.max(0,wbAssemblyCount);
 const total=n+rest;
 const pct=total>0?n/total*100:0;
 document.getElementById('remaining').textContent=rest;
 document.getElementById('progressPlan').textContent=' / '+total+' под контролем';
 document.getElementById('progressBar').style.width=pct.toFixed(1)+'%';
 document.getElementById('progressText').textContent='Собрано сегодня: '+n+' · остаток заказов WB за сегодня: '+rest+
  ' · '+pct.toFixed(1)+'% условного объёма. Факт по дате сканирования, остаток по дате заказа; это не дневной план.';
}

async function loadWbAssemblyPlan(){
 const info=document.getElementById('wbAssemblyInfo');
 try{
  const response=await fetchWithTimeout('/api/manager/wb-fbs/picking-queue?day='+encodeURIComponent(dayKey()),{cache:'no-store'},5000);
  if(response.status===403){location.href='/manager-login';return}
  if(!response.ok)throw Error('HTTP '+response.status);
  const result=await response.json();
  if(result.state!=='connected'||!Number.isInteger(result.remaining_orders)||
     !Number.isInteger(result.confirm_orders)||!Number.isInteger(result.delivery_orders)){
   throw Error('Очередь WB недоступна');
  }
  wbAssemblyCount=result.remaining_orders;
  renderProgress(Number(document.getElementById('todayOrders').textContent||0));
  info.textContent='Очередь WB за сегодня: на сборке '+result.confirm_orders+
    ' · в доставке WB '+result.delivery_orders+' · обновляется автоматически';
 }catch(e){
  wbAssemblyCount=null;
  renderProgress(Number(document.getElementById('todayOrders').textContent||0));
  info.textContent='Очередь WB недоступна · общий объём не рассчитан';
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
 const [dashR,fbsR,recR,devR,hourR,histR]=await Promise.all([
  fetchWithTimeout('/api/fbs/dashboard?'+p.toString()),
  fetchWithTimeout('/api/fbs/overview?'+p.toString()),
  fetchWithTimeout('/api/receiving/overview?'+p.toString()),
  fetchWithTimeout('/api/operations/deviations?'+p.toString()),
  fetchWithTimeout('/api/fbs/hourly?'+p.toString()),
  fetchWithTimeout('/api/manager/integrations/wildberries/history?date='+encodeURIComponent(date)).catch(()=>null)
 ]);
 if([dashR,fbsR,recR,devR,hourR].some(r=>r.status===403)){location.href='/manager-login';return}
 const d=await dashR.json(),f=await fbsR.json(),r=await recR.json(),dev=await devR.json(),hour=await hourR.json();
 const hist=histR&&histR.ok?await histR.json():null;
 const picked=hist&&Number.isInteger(hist.picked_in_grit)?hist.picked_in_grit:null;

 const fact=picked!==null?picked:Number(f.orders||0);
 document.getElementById('todayOrders').textContent=fact;
 document.getElementById('wbDailyFlow').textContent=picked!==null
  ?'За сегодня · поступило на сборку '+hist.new_on_assembly_observed+
    ' · собрано '+picked+' · замечено переходов в доставку '+hist.moved_to_delivery_observed
  :'История WB недоступна · факт показан по журналу FBS';
 document.getElementById('receivingUnits').textContent=Math.round(Number(r.units||0));
 document.getElementById('deviationCount').textContent=dev.count||0;
 document.getElementById('active').textContent=d.workers.length;
 renderProgress(fact);
 renderHourly(hour.rows||[]);

 const box=document.getElementById('workers');
 box.innerHTML=d.workers.length?d.workers.map(x=>renderEmployeeShift(x,false)).join(''):'<div class="empty-state"><svg class="empty-icon" aria-hidden="true"><use href="#ic-users"></use></svg><strong>Нет активных смен</strong><span>Сотрудники появятся после начала работы</span></div>';
 const completed=d.completed||[];
 document.getElementById('completedCard').style.display=completed.length?'':'none';
 document.getElementById('completedWorkers').innerHTML=completed.map(x=>renderEmployeeShift(x,true)).join('');

 document.getElementById('receivingOps').textContent=r.operations||0;
 document.getElementById('receivingWorkers').textContent=r.workers||0;
 document.getElementById('receivingCompleted').textContent=r.completed||0;

 document.getElementById('deviations').innerHTML=dev.rows.length?dev.rows.slice(0,8).map(x=>'<div class="dev"><div>'+esc(fmt(x.created_at))+'</div><div><b>'+esc(x.employee)+'</b></div><div class="why '+deviationClass(x.event_type)+'">'+esc(deviationLabel(x.event_type))+'</div><div class="object">'+esc(x.article||x.object_code||x.result||'—')+'</div></div>').join(''):'<div class="empty-state"><svg class="empty-icon" aria-hidden="true"><use href="#ic-check"></use></svg><strong>Отклонений сегодня нет</strong><span>Новых ошибок и предупреждений не зарегистрировано</span></div>';
}

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
async function wbAssemblyLoop(){if(!document.hidden)await loadWbAssemblyPlan();setTimeout(wbAssemblyLoop,15000)}
wbAssemblyLoop();
document.addEventListener('visibilitychange',()=>{if(!document.hidden){runDashboardLoad();runWbStatus()}});
if(sessionStorage.getItem('openExitQr')==='1'){sessionStorage.removeItem('openExitQr');setTimeout(openExitQr,250)}
</script></body></html>"""


SHIFT_ADMIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Управление сменами</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;color:#25313d;margin:0}
.wrap{max-width:1040px;margin:auto;padding:18px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:15px;padding:18px;margin:14px 0}
a{color:#456981}h1{margin:6px 0 14px}h2{font-size:18px;margin:0 0 12px}.small{color:#667989;font-size:13px}
.note{background:#fff3da;border:1px solid #e6cea5;padding:12px;border-radius:10px;margin:12px 0}
.shift{display:flex;justify-content:space-between;align-items:center;gap:16px;border-top:1px solid #dbe4eb;padding:12px 0}.shift:first-child{border-top:0}
.shift p{margin:4px 0}.history{border-top:1px solid #dbe4eb;padding:10px 0;overflow-wrap:anywhere}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.entry{display:grid;gap:6px;margin-bottom:12px}
input,textarea{width:100%;padding:10px;border:1px solid #cbd9e2;background:white;border-radius:9px;font:inherit}
button{border:0;border-radius:9px;background:#48647a;color:white;padding:10px 14px;font-weight:700;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}.secondary{background:#dae5ec;color:#314657}
.error{color:#a33b3b}.ok{color:#287347}.details{font-size:13px;color:#667989}
#editor{display:none;border-top:1px solid #dbe4eb;margin-top:12px;padding-top:16px}
@media(max-width:680px){.row{grid-template-columns:1fr}.shift{align-items:flex-start;flex-direction:column}}
</style></head><body><main class="wrap">
<a href="/manager">← Панель управления</a><h1>Управление сменами</h1>
<div class="note">Только для исправления зависших смен. Указывайте фактическое время окончания. Сканирования и количество собранных заказов не изменяются.</div>
<section class="card"><h2>Незакрытые смены</h2><div id="shiftRows">Загружаю…</div>
<div id="editor"><h2>Принудительно завершить смену</h2><div id="selected" class="details"></div>
<div class="row">
<label class="entry">Фактическая дата и время окончания<input id="actualEnd" type="datetime-local" step="60" required></label>
<label class="entry">Причина<textarea id="reason" rows="3" maxlength="500" placeholder="Например: QR не закрыл смену" required></textarea></label>
</div><p class="small">Время не должно быть раньше уже записанной операции или позже текущего времени.</p>
<button id="confirm" type="button">Подтвердить закрытие</button> <button class="secondary" id="cancel" type="button">Отмена</button>
<div role="status" id="feedback"></div></div></section>
<section class="card"><h2>Журнал исправлений</h2><div id="historyRows">Загружаю…</div></section>
</main><script>
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]})}
function fmt(s){return s?new Date(s).toLocaleString('ru-RU'):'—'}
let shifts=[],selected=null,saving=false;
async function load(){
 const pair=await Promise.all([fetch('/api/fbs/dashboard'),fetch('/api/manager/shifts/actions')]);
 if(pair.some(function(r){return r.status===403})){location.href='/manager-login';return}
 if(pair.some(function(r){return !r.ok}))throw Error('Не удалось загрузить смены');
 const d=await Promise.all(pair.map(function(r){return r.json()}));
 shifts=d[0].workers||[];
 document.getElementById('shiftRows').innerHTML=shifts.length?shifts.map(function(w){
  return '<div class="shift"><div><b>'+esc(w.employee)+'</b><p class="details">Начало: '+esc(fmt(w.started_at))+' · '+esc(w.activity||'—')+'</p><p class="details">Смена: '+esc(w.shift_id)+'</p></div><button type="button" data-shift="'+esc(w.shift_id)+'">Завершить смену</button></div>'
 }).join(''):'<p class="small">Открытых смен нет.</p>';
 const rows=d[1].actions||[];
 document.getElementById('historyRows').innerHTML=rows.length?rows.map(function(a){
  return '<div class="history"><b>'+esc(a.employee)+'</b> · завершена '+esc(fmt(a.actual_ended_at))+
  '<div class="details">Изменено '+esc(fmt(a.recorded_at))+' · '+esc(a.admin_name)+'</div><div>'+esc(a.reason)+'</div></div>'
 }).join(''):'<p class="small">Исправлений ещё нет.</p>';
}
document.getElementById('shiftRows').addEventListener('click',function(e){
 const b=e.target.closest('button[data-shift]');if(!b)return;
 selected=shifts.find(function(w){return w.shift_id===b.dataset.shift});if(!selected)return;
 document.getElementById('selected').textContent=selected.employee+' · начало: '+fmt(selected.started_at);
 document.getElementById('actualEnd').value='';document.getElementById('reason').value='';
 document.getElementById('feedback').textContent='';document.getElementById('editor').style.display='block';
});
document.getElementById('cancel').addEventListener('click',function(){selected=null;document.getElementById('editor').style.display='none'});
document.getElementById('confirm').addEventListener('click',async function(){
 if(!selected||saving)return;
 const value=document.getElementById('actualEnd').value,reason=document.getElementById('reason').value.trim();
 const feedback=document.getElementById('feedback');
 if(!value||reason.length<8){feedback.className='error';feedback.textContent='Укажи дату, время и причину (не менее 8 символов).';return}
 const end=new Date(value);if(isNaN(end.getTime())){feedback.className='error';feedback.textContent='Неверная дата.';return}
 if(!confirm('Закрыть смену '+selected.employee+' в '+fmt(end.toISOString())+'? Исправление попадёт в журнал.'))return;
 saving=true;this.disabled=true;feedback.textContent='Сохраняю…';feedback.className='';
 try{
  const r=await fetch('/api/manager/shifts/force-close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({shift_id:selected.shift_id,actual_ended_at:end.toISOString(),reason:reason})});
  const d=await r.json();
  if(!r.ok){feedback.className='error';feedback.textContent=d.error||'Не удалось завершить смену';return}
  feedback.className='ok';feedback.textContent='Смена завершена, действие записано в журнал.';
  selected=null;await load();
  setTimeout(function(){document.getElementById('editor').style.display='none'},1200);
 }catch(e){feedback.className='error';feedback.textContent='Ошибка запроса. Обнови список перед повтором.'}
 finally{saving=false;document.getElementById('confirm').disabled=false}
});
load().catch(function(e){document.getElementById('shiftRows').textContent=e.message});
</script></body></html>"""


RECEIVING_MONITOR_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Приёмка</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100dvh}.sidebar{background:#f7f9fb;border-right:1px solid #d7e0e7;padding:18px 12px;position:sticky;top:0;height:100dvh}.brand{padding:4px 8px 16px;border-bottom:1px solid #e0e7ec;margin-bottom:12px}.brand b{display:block;font-size:18px}.brand span{font-size:11px;color:#6b7785}.side-nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:9px;min-height:44px;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:750;font-size:13px;background:#eef3f6;color:#314657;border:1px solid transparent}.side-nav a:hover{background:#e3ebf0}.side-nav a.active{background:#48647a;color:#fff}.side-nav a.logout{margin-top:12px;background:#f3ecec;color:#794545}
.content{min-width:0;padding:16px}.wrap{max-width:1500px;margin:auto}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(52,72,91,.07);margin-bottom:12px}
h1{margin:0;font-size:28px}.small{font-size:12px;color:#6b7785}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}.kpi{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:15px}.kpi .v{font-size:30px;font-weight:850;margin-top:4px}.kpi .l{font-size:12px;color:#6b7785}
.table-wrap{overflow:auto;max-height:68vh;border:1px solid #dde5eb;border-radius:11px}table{width:100%;border-collapse:collapse;min-width:1050px;background:#fff}th,td{padding:10px 11px;border-bottom:1px solid #e4eaee;text-align:left;font-size:13px;white-space:nowrap}th{position:sticky;top:0;background:#eef3f6;z-index:2;color:#51606e}.article{font-weight:800}.muted{color:#71808d}.qty{font-weight:850}
.period-filters{display:flex;align-items:flex-end;gap:9px;flex-wrap:wrap;margin-top:14px}.period-filters label{display:grid;gap:4px;font-size:12px;font-weight:700;color:#51606e}.period-filters input{min-height:39px;border:1px solid #cbd9e2;border-radius:9px;background:#fff;padding:7px 10px;color:#25313d;font:inherit}.period-filters button{min-height:39px;border:1px solid #cbd9e2;border-radius:9px;background:#48647a;color:#fff;padding:8px 13px;font-weight:750;cursor:pointer}.period-filters button.secondary{background:#e8eef3;color:#314657}.period-message{margin-top:9px}.data-warning{margin-top:7px;color:#8a5c2e}
@media(max-width:1000px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid #d7e0e7}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a.logout{margin-top:0}.content{padding:12px}.kpis{grid-template-columns:1fr 1fr}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body>
<div class="app">
<aside class="sidebar">
 <div class="brand"><b><img src="/grit-logo.png?v=2" width="24" height="24" alt="" style="vertical-align:middle;border-radius:5px;margin-right:6px">GRIT WMS</b><span>Панель управления</span></div>
 <nav class="side-nav">
  <a href="/manager">🖥️ Главная</a>
  <a href="/fbs-journal">📋 Журнал FBS</a>
  <a href="/history">🔎 История ШК</a>
  <a class="active" href="/receiving-monitor">📥 Приёмка</a>
  <a href="/manager/integrations">🔌 Интеграции</a>
  <a href="/badges">🪪 Бейджи</a>
  <a href="/manager#qr" onclick="sessionStorage.setItem('openExitQr','1')">🔐 QR выхода</a>
  <a class="logout" href="#" onclick="managerLogout();return false">🚪 Выйти из ПК</a>
 </nav>
</aside>
<main class="content"><div class="wrap">
 <div class="card"><h1>📥 Приёмка</h1><div class="small">История операций приёмки. Обновление каждые 10 секунд.</div>
  <div class="period-filters">
   <label>С даты<input id="dateFrom" type="date" aria-label="Начало периода"></label>
   <label>По дату<input id="dateTo" type="date" aria-label="Конец периода"></label>
   <button id="showPeriod" type="button">Показать</button>
   <button id="showToday" class="secondary" type="button">Сегодня</button>
   <button id="showYesterday" class="secondary" type="button">Вчера</button>
  </div>
  <div id="periodMessage" class="small period-message"></div>
  <div class="small data-warning">Пока показатели рассчитаны по событиям GRIT: незавершённые документы МойСклад могут попадать в итог. Сверка будет исправлена отдельно.</div>
 </div>
 <div class="kpis">
  <div class="kpi"><div class="l">Единиц по операциям за период</div><div class="v" id="units">0</div></div>
  <div class="kpi"><div class="l">Операций приёмки</div><div class="v" id="operations">0</div></div>
  <div class="kpi"><div class="l">Приёмщиков за период</div><div class="v" id="workers">0</div></div>
  <div class="kpi"><div class="l">Событий завершения документов</div><div class="v" id="completed">0</div></div>
 </div>
 <div class="card">
  <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px"><b>Последние операции приёмки</b><span class="small">До 200 последних строк за выбранный период</span><span class="small">палетный ШК / ОСЗ / неполная палета</span></div>
  <div class="table-wrap"><table><thead><tr><th>Дата и время</th><th>Сотрудник</th><th>Артикул</th><th>Товар</th><th>ШК / SSCC</th><th>Количество</th><th>Документ</th><th>Тип</th></tr></thead><tbody id="rows"></tbody></table></div>
 </div>
</div></main></div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return ts}}
function dateKey(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function periodBounds(start,end){const s=new Date(start+'T00:00:00'),e=new Date(end+'T00:00:00');e.setDate(e.getDate()+1);return {from:s.toISOString(),to:e.toISOString()}}
let selectedBounds=null;
function applyPeriod(refresh=true){
 const start=document.getElementById('dateFrom').value,end=document.getElementById('dateTo').value;
 const note=document.getElementById('periodMessage');
 if(!start||!end||start>end){note.textContent='Укажите корректный период: дата начала не должна быть позже даты окончания.';return}
 selectedBounds=periodBounds(start,end);
 note.textContent='Период: '+start.split('-').reverse().join('.')+' — '+end.split('-').reverse().join('.');
 if(refresh)load().catch(()=>{note.textContent+=' · Не удалось получить данные, попробуйте обновить страницу.'});
}
function pickDay(offset,refresh=true){
 const d=new Date();d.setDate(d.getDate()+offset);
 const value=dateKey(d);
 document.getElementById('dateFrom').value=value;
 document.getElementById('dateTo').value=value;
 applyPeriod(refresh);
}
function typeLabel(t){return t==='external_pallet'?'ОСЗ / SSCC':t==='scan'?'Палета / товар':t==='complete'?'Завершение':'Операция'}
async function load(){
 const p=new URLSearchParams(selectedBounds),r=await fetch('/api/receiving/overview?'+p.toString());
 if(!r.ok&&r.status!==403)throw new Error('Ошибка загрузки истории приёмки: '+r.status);
 if(r.status===403){location.href='/manager-login';return}
 const d=await r.json();
 document.getElementById('units').textContent=d.units||0;
 document.getElementById('operations').textContent=d.operations||0;
 document.getElementById('workers').textContent=d.workers||0;
 document.getElementById('completed').textContent=d.completed||0;
 document.getElementById('rows').innerHTML=d.recent.length?d.recent.map(x=>'<tr><td>'+esc(fmt(x.created_at))+'</td><td><b>'+esc(x.employee)+'</b></td><td class="article">'+esc(x.article||'—')+'</td><td>'+esc(x.product_name||'—')+'</td><td class="muted">'+esc(x.object_code||'—')+'</td><td class="qty">'+esc(x.quantity??'—')+'</td><td>'+esc(x.document_name||x.document_id||'—')+'</td><td>'+esc(typeLabel(x.event_type))+'</td></tr>').join(''):'<tr><td colspan="8">За выбранный период приёмок нет</td></tr>';
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
document.getElementById('showPeriod').addEventListener('click',()=>applyPeriod());
document.getElementById('showToday').addEventListener('click',()=>pickDay(0));
document.getElementById('showYesterday').addEventListener('click',()=>pickDay(-1));
pickDay(0,false);
monitorLoop();
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!monitorBusy)monitorLoop()});
</script></body></html>"""


FBS_JOURNAL_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Журнал FBS</title>
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
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap">
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
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · История ШК</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.wrap{max-width:1450px;margin:auto;padding:14px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px}.top a{padding:9px 12px;border-radius:9px;background:#dde8ef;color:#314657;text-decoration:none;font-weight:750}
h1{margin:0;font-size:24px}.small{font-size:12px;color:#6b7785}.search{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:12px}
input,button{min-height:44px;border:1px solid #cbd9e2;border-radius:9px;padding:9px 11px;font-size:15px}button{background:#48647a;color:#fff;font-weight:800;cursor:pointer}
.timeline{margin-top:12px}.event{display:grid;grid-template-columns:155px 130px 160px minmax(0,1fr);gap:10px;padding:11px 0;border-bottom:1px solid #e1e7eb;align-items:center}.event:last-child{border:0}
.time{font-size:12px;color:#6b7785}.who{font-weight:800}.type{font-weight:800}.meta{font-size:12px}.empty{padding:22px 0;color:#6b7785}
@media(max-width:800px){.event{grid-template-columns:1fr 1fr}.meta{grid-column:1/-1}.top{align-items:flex-start;flex-direction:column}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap">
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
function label(t){return ({scan:'Приёмка / скан',external_pallet:'ОСЗ / SSCC',complete:'Документ завершён',start:'Начало смены',end:'Конец смены',switch:'Смена операции',duplicate_wb:'Повторный WB',wb_same_as_product:'ШК товара вместо WB',product_not_found:'Товар не найден',product_mismatch:'Не тот товар',scan_rejected:'Скан отклонён',fbs_scan:'FBS заказ'})[t]||t||'Событие'}
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
<meta name="theme-color" content="#09121e"><title>GRIT WMS · Бейджи</title>
<style>
:root{color-scheme:dark;--bg:#09121e;--panel:#122438;--panel-deep:#101e2d;--line:#314d64;--text:#eef6ff;--muted:#a4bbce;--blue:#176eae;--orange:#ff983b}
*{box-sizing:border-box}
html{min-height:100%;background:var(--bg)}
body{font:14px/1.48 system-ui,-apple-system,"Segoe UI",Arial,sans-serif;margin:0;min-height:100vh;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
.wrap{max-width:1060px;margin:auto;padding:20px 24px 40px}
.top{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px}
.top a,.top button,.print-one{display:inline-flex;align-items:center;justify-content:center;padding:10px 15px;border:1px solid #438ac1;border-radius:9px;background:var(--blue);color:#f7fbff;text-decoration:none;font-weight:750;cursor:pointer;font:inherit}
.top a{background:#203a50;border-color:#365976;color:#cae7ff}
.top a:hover{background:#2a516c}
.top button:hover,.print-one:hover{background:#2488cf}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.badge{background:linear-gradient(140deg,#14273b,#0f1e2e);border:1px solid var(--line);border-radius:14px;padding:20px;text-align:center;display:flex;flex-direction:column;align-items:center;min-width:0}
.brand{font-size:11px;color:var(--muted);letter-spacing:.05em}
.name{font-size:21px;font-weight:800;margin-top:8px;overflow-wrap:anywhere}
.qr{width:220px;height:220px;max-width:100%;margin:16px auto 12px;display:block;image-rendering:pixelated;background:#fff;padding:8px;border-radius:4px}
.num{font-size:18px;font-weight:800}
.print-one{margin-top:15px;width:100%}
.status{background:var(--panel);border:1px solid var(--line);border-radius:13px;padding:18px;color:var(--text)}
.manage{display:flex;width:100%;gap:8px;margin-top:10px}
.manage button{flex:1;background:#203a50;color:#cae7ff;border:1px solid #365976;border-radius:9px;padding:10px;cursor:pointer;font-weight:700}
.manage button:hover{background:#2a516c}
.manage button.archive{background:#462b36;color:#ffb8bb;border-color:#744250}
.manage button.archive:hover{background:#60343d}
.create{background:linear-gradient(140deg,#14273b,#0f1e2e);border:1px solid var(--line);border-radius:13px;padding:15px;margin-bottom:15px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.create label{color:var(--text);white-space:nowrap}
.create input{flex:1;min-width:180px;background:#0b1927;color:var(--text);border:1px solid #365976;padding:12px;border-radius:9px;font:inherit}
.create input::placeholder{color:#94adbf}
.create input:focus{outline:2px solid var(--orange);outline-offset:1px}
.create button{background:var(--blue);color:white;border:1px solid #438ac1;border-radius:9px;padding:12px;font-weight:750;cursor:pointer}
.create button:hover{background:#2488cf}
.archived{background:linear-gradient(140deg,#14273b,#0f1e2e);border:1px solid var(--line);border-radius:13px;margin-top:20px;padding:18px}
.archived h3{margin:0 0 5px}
.archived p{color:var(--muted);margin:0 0 14px}
.archived-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px;border-top:1px solid #294158;overflow-wrap:anywhere}
.archived-row button{background:#203a50;color:#cae7ff;border:1px solid #365976;border-radius:8px;padding:9px;cursor:pointer}
.message{margin:10px 0;color:#77c0ff;font-weight:650}
.badge button:disabled,.create button:disabled{opacity:.5;cursor:wait}
button:focus-visible,a:focus-visible{outline:2px solid var(--orange);outline-offset:2px}
@media(max-width:620px){.wrap{padding:14px}.create{align-items:stretch}.create label{width:100%}.create input{width:100%}.create button{width:100%}.grid{grid-template-columns:1fr}}
@media print{
 :root{color-scheme:light}
 html,body{background:#fff!important;color:#111!important}
 .wrap{padding:0}
 .top,.print-one,.manage,.create,.archived,.message{display:none!important}
 .badge{break-inside:avoid;box-shadow:none;background:#fff!important;border:1px solid #ddd!important;color:#111!important}
 .brand{color:#333!important}
 .name,.num{color:#111!important}
 .qr{padding:0}
 body.print-single .badge{display:none!important}
 body.print-single .badge.print-target{display:flex!important;margin:0 auto;max-width:360px;border:0!important}
}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body>
<div class="wrap">
 <div class="top"><a href="/stats">← Статистика</a><button type="button" onclick="printAllBadges()">Печать всех · 58×40</button></div>
 <form id="createEmployee" class="create"><label for="employeeName"><b>Новый сотрудник</b></label><input id="employeeName" maxlength="80" minlength="2" required placeholder="Имя сотрудника"><button type="submit">Добавить и создать QR</button></form>
 <div id="message" class="message" role="status" aria-live="polite"></div>
 <div id="grid" class="grid"></div>
 <section class="archived"><h3>Архивные бейджи</h3><p>История смен сохраняется. Архивный бейдж не позволяет войти.</p><div id="archiveList"></div></section>
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
  const d=await fetchJson('/api/admin/badges?refresh='+Date.now(),5000);
  if(!d)return;
  g.innerHTML=d.employees.map(x=>
   '<div class="badge" id="badge_'+esc(x.id)+'">'+
    '<div class="brand">GRIT WMS</div>'+
    '<div class="name">'+esc(x.name)+'</div>'+
    '<img class="qr" src="'+x.qr_png+'" alt="QR '+esc(x.name)+'">'+
    '<div class="num">№'+esc(x.badge_number)+'</div>'+
    '<button type="button" class="print-one" data-employee-id="'+esc(x.id)+'" onclick="printBadge(this.dataset.employeeId)">Печать 58×40</button>'+
    '<div class="manage"><button type="button" data-action="rename" data-id="'+esc(x.id)+'">Переименовать</button><button type="button" class="archive" data-action="archive" data-id="'+esc(x.id)+'">Удалить</button></div>'+
   '</div>'
  ).join('')||'<div class="status">Активных бейджей нет.</div>';
  document.getElementById('archiveList').innerHTML=(d.archived||[]).map(x=>
   '<div class="archived-row"><span>'+esc(x.name)+' · №'+esc(x.badge_number)+'</span><button type="button" data-action="restore" data-id="'+esc(x.id)+'">Восстановить</button></div>'
  ).join('')||'Нет архивных сотрудников.';
 }catch(e){
  g.innerHTML='<div class="status"><b>Не удалось загрузить бейджи</b><div style="margin-top:6px;font-size:12px;color:#6b7785">Обнови страницу. Если ошибка повторится, проверь журнал ONIX.</div></div>';
 }
}
async function mutate(url,method,data){
 const r=await fetch(url,{method,headers:{'Content-Type':'application/json'},body:JSON.stringify(data||{}),cache:'no-store'});
 const d=await r.json();
 if(r.status===403 && !d.error){location.href='/manager-login';return null}
 if(!r.ok)throw Error(d.error||'Не удалось сохранить изменение');
 return d;
}
const info=message=>document.getElementById('message').textContent=message;
document.getElementById('createEmployee').addEventListener('submit',async event=>{
 event.preventDefault();
 const name=document.getElementById('employeeName').value.trim();
 if(name.length<2)return;
 const button=event.currentTarget.querySelector('button');
 button.disabled=true;
 try{
  const result=await mutate('/api/admin/badges','POST',{name});
  if(result){document.getElementById('employeeName').value='';await load();info('Бейдж №'+result.badge_number+' создан. Распечатай QR для сотрудника.')}
 }catch(error){info(error.message)}finally{button.disabled=false}
});
document.getElementById('grid').addEventListener('click',handleBadgeAction);
document.getElementById('archiveList').addEventListener('click',handleBadgeAction);
async function handleBadgeAction(event){
 const button=event.target.closest('button[data-action]');
 if(!button)return;
 const id=button.dataset.id,action=button.dataset.action;
 const card=button.closest('.badge'), row=button.closest('.archived-row');
 const currentName=card?.querySelector('.name')?.textContent||row?.querySelector('span')?.textContent||'';
 let name,method,url,data={},success;
 if(action==='rename'){
  name=prompt('Новое имя сотрудника',currentName);
  if(name===null)return;
  name=name.trim();if(name.length<2)return info('Укажи имя минимум из двух символов');
  method='PATCH';url='/api/admin/badges/'+encodeURIComponent(id);data={name};success='Имя изменено. Старый QR продолжает работать.';
 }else if(action==='archive'){
  if(!confirm('Удалить бейдж сотрудника «'+currentName+'» из активных? История смен сохранится, вход по QR будет закрыт.'))return;
  method='POST';url='/api/admin/badges/'+encodeURIComponent(id)+'/archive';success='Бейдж перемещён в архив.';
 }else if(action==='restore'){
  if(!confirm('Восстановить сотрудника «'+currentName+'»? Сохранённый QR снова станет действительным.'))return;
  method='POST';url='/api/admin/badges/'+encodeURIComponent(id)+'/restore';success='Сотрудник восстановлен.';
 }else{return}
 button.disabled=true;
 try{
  const result=await mutate(url,method,data);
  if(!result)return;
  if(action==='rename'){
   if(result.name!==name)throw Error('Сервер не подтвердил новое имя. Изменение не выполнено.');
   const title=card?.querySelector('.name');
   if(title)title.textContent=result.name;
  }
  await load();
  if(action==='rename'){
   const saved=document.getElementById('badge_'+id)?.querySelector('.name')?.textContent;
   if(saved!==name)throw Error('При обновлении отображается старое имя. Сохранение требует проверки.');
  }
  info(success);
 }catch(error){info(error.message)}
 finally{button.disabled=false}
}
function clearPrintSelection(){
 document.body.classList.remove('print-single');
 document.querySelectorAll('.badge.print-target').forEach(el=>el.classList.remove('print-target'));
}
function printAllBadges(){window.open('/manager/badges/labels/all','_blank','noopener')}
function printBadge(id){
 if(!id)return;
 window.open('/manager/badges/label?id='+encodeURIComponent(id),'_blank','noopener');
}
window.addEventListener('afterprint',clearPrintSelection);
load();
</script></body></html>"""


@wms_bp.get("/setup/viktor-8f4c2e91a7b6")
def bootstrap_viktor_badge():
    ensure_db()
    row = query_one("SELECT badge_code FROM employees WHERE id = ?", ("viktor",))
    code = str((row[0] if row else "") or "").strip() or make_badge_code("viktor")
    page = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>GRIT WMS · Бейдж Виктора</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#e9eef3;color:#25313d}.wrap{max-width:520px;margin:auto;padding:18px}.card{background:#fff;border:1px solid #d7e0e7;border-radius:18px;padding:22px;text-align:center}.qr{display:flex;justify-content:center;margin:18px 0}.num{font-size:34px;font-weight:850}.small{font-size:12px;color:#6b7785}.btn{display:block;margin-top:14px;padding:12px;border-radius:11px;background:#48647a;color:#fff;text-decoration:none;font-weight:750}@media print{body{background:#fff}.wrap{padding:0}.btn,.hint{display:none}.card{border:0}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap"><div class="card">
<div class="small">GRIT WMS</div><div class="num">№1</div><h2>Виктор</h2><div class="small">Руководитель склада · Manager</div>
<div id="qr" class="qr"></div><div class="small">Личный бейдж входа</div>
<a class="btn" href="/login">Перейти ко входу</a><div class="small hint" style="margin-top:12px">Сохрани или распечатай этот QR. Он предназначен для входа Виктора в GRIT WMS.</div>
</div></div><script>new QRCode(document.getElementById('qr'),{text:"__VIKTOR_BADGE_CODE__",width:260,height:260});</script></body></html>"""
    return page.replace("__VIKTOR_BADGE_CODE__", code)


@wms_bp.get("/login")
def login_page():
    ensure_db()
    # QR attendance must resume after badge login, including existing sessions.
    requested_next = request.args.get("next", "")
    attendance_next = requested_next if requested_next in ("/attendance/in", "/attendance/out") else None
    employee = get_session_employee()
    if employee and attendance_next:
        return redirect(attendance_next)
    if employee:
        # On browser restart return to the operation this employee was actually
        # doing, rather than always redirecting a receiver to FBS picking.
        shift = query_one(
            "SELECT id FROM shifts WHERE employee_id = ? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
            (employee["id"],),
        )
        active = current_activity(shift[0]) if shift else None
        target = {
            "receiving": "/",
            "replenishment": "/replenishment",
            "movement": "/move",
        }.get(active["type"] if active else "", "/fbs")
        return redirect(target)
    return LOGIN_PAGE


def normalize_badge_input(value):
    code = str(value or "").strip()
    # HID scanners can prepend AIM symbology identifiers such as ]C0 / ]C1.
    if len(code) >= 3 and code[0] == "]" and code[1].isalpha() and code[2].isdigit():
        code = code[3:]
    code = code.replace("\r", "").replace("\n", "").replace("\t", "").strip()

    # iOS physical Bluetooth HID keyboard may use Russian layout.
    # Decode ONLY a full WMS-BADGE token, never arbitrary product codes.
    if code.casefold().startswith("цьы-ифвпу-"):
        ru_keys = "йцукенгшщзхъфывапролджэячсмитьбю"
        us_keys = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
        decoded = code.lower().translate(str.maketrans(ru_keys, us_keys))
        suffix = decoded[len("wms-badge-"):]
        if (decoded.startswith("wms-badge-") and len(suffix) == 32
                and all(ch in "0123456789abcdef" for ch in suffix)):
            return "WMS-BADGE-" + suffix
    return code


@wms_bp.post("/api/auth/login")
def auth_login():
    ensure_db()
    # One shared terminal, one employee. No badge can overwrite an occupied
    # session, even by calling the JSON endpoint directly.
    current = get_session_employee()
    if current:
        return jsonify(
            error="Терминал занят: " + current["name"] + ". Сотрудник должен завершить смену через QR ВЫХОД.",
            occupied=True,
            employee=current["name"],
        ), 409
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
    session.permanent = True
    shift_id, _ = ensure_active_shift(row[0], "other")
    session["employee_shift_id"] = shift_id
    log_event(row[0], "auth", "login", shift_id=shift_id, object_code=badge_code, result="ok")
    return jsonify(ok=True, shift_id=shift_id, employee={"id": row[0], "name": row[1], "role": row[2], "badge_number": row[3]})


@wms_bp.get("/exit")
def exit_page():
    # Retired supervisor-QR exit screen. Existing navigation points to OUT.
    return redirect("/attendance/out", code=303)


@wms_bp.get("/supervisor-terminal/setup")
def supervisor_terminal_setup_page():
    return redirect("/manager/attendance/qr", code=303)


@wms_bp.post("/api/supervisor-terminal/activate")
def supervisor_terminal_activate():
    return jsonify(error="QR старшего отключён."), 410


@wms_bp.get("/supervisor-terminal")
def supervisor_terminal_page():
    return redirect("/manager/attendance/qr", code=303)


@wms_bp.get("/api/supervisor-terminal/qr")
def supervisor_terminal_qr():
    return jsonify(error="QR старшего отключён."), 410


@wms_bp.get("/supervisor-qr")
def supervisor_qr_page():
    # Deprecated: QR старшего больше не нужен для завершения смены.
    return redirect("/manager", code=303)


@wms_bp.get("/api/admin/supervisor-exit-code")
def supervisor_exit_code_api():
    # Old QR code must not be minted for regular employee exits.
    return jsonify(error="QR старшего отключён. Используйте QR ВЫХОД."), 410


@wms_bp.post("/api/auth/logout")
def auth_logout():
    # Prevent legacy logout from bypassing attendance records and shift closure.
    return jsonify(error="Используйте QR ВЫХОД для закрытия смены."), 410


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
    if get_session_employee() and not session.get("manager_web"):
        return redirect("/fbs")
    if manager_access():
        return redirect("/manager")
    return MANAGER_LOGIN_PAGE


@wms_bp.post("/api/manager/login")
def manager_login_api():
    # The manager password must not silently replace an employee on the
    # shared phone. Manager PC sessions without a badge are unaffected.
    current = get_session_employee()
    if current:
        return jsonify(
            error="Терминал занят: " + current["name"] + ". Выйди по QR старшего перед сменой учётной записи.",
            occupied=True,
        ), 409
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
    session["manager_login_at"] = time.time()
    session.permanent = True
    return jsonify(ok=True)


@wms_bp.post("/api/manager/logout")
def manager_logout_api():
    session.pop("manager_web", None)
    session.pop("manager_login_at", None)
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


@wms_bp.get("/manager/shifts")
def manager_shift_admin_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return SHIFT_ADMIN_PAGE


@wms_bp.get("/api/manager/shifts/actions")
def manager_shift_actions_api():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    rows = query_all(
        """
        SELECT a.shift_id, e.name, a.admin_name, a.reason, a.actual_ended_at, a.recorded_at
        FROM shift_admin_actions a JOIN employees e ON e.id = a.employee_id
        ORDER BY a.recorded_at DESC LIMIT 50
        """
    )
    return jsonify(actions=[
        {"shift_id": r[0], "employee": r[1], "admin_name": r[2],
         "reason": r[3], "actual_ended_at": r[4], "recorded_at": r[5]}
        for r in rows
    ])


@wms_bp.post("/api/manager/shifts/force-close")
def manager_force_close_shift():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not request.is_json:
        return jsonify(error="Ожидается JSON"), 415
    data = request.get_json(silent=True) or {}
    shift_id = str(data.get("shift_id") or "").strip()
    reason = str(data.get("reason") or "").strip()
    raw_end = str(data.get("actual_ended_at") or "").strip()
    if not shift_id or not 8 <= len(reason) <= 500:
        return jsonify(error="Укажи смену и причину от 8 до 500 символов"), 400
    try:
        actual = parse_iso(raw_end)
        if actual is None or actual.utcoffset() is None:
            raise ValueError("Missing timezone")
        actual = actual.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return jsonify(error="Неверное время окончания"), 400
    if actual > datetime.now(timezone.utc):
        return jsonify(error="Время окончания не может быть в будущем"), 400
    end_iso = actual.isoformat()
    actor = get_session_employee()
    admin_name = actor["name"] if actor and actor["role"] == "manager" else "Руководитель (ПК)"
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if not DATABASE_URL:
                cur.execute("BEGIN IMMEDIATE")
            cur.execute(sql("SELECT employee_id, started_at, ended_at FROM shifts WHERE id = ?" + (" FOR UPDATE" if DATABASE_URL else "")), (shift_id,))
            shift = cur.fetchone()
            if shift is None:
                return jsonify(error="Смена не найдена"), 404
            if shift[2] is not None:
                return jsonify(error="Смена уже завершена"), 409
            started = parse_iso(shift[1])
            if started is None or started.utcoffset() is None or actual < started.astimezone(timezone.utc):
                return jsonify(error="Дата окончания раньше начала смены"), 400

            cur.execute(sql("SELECT MAX(started_at), MAX(ended_at) FROM activities WHERE shift_id = ?"), (shift_id,))
            times = list(cur.fetchone())
            cur.execute(sql("SELECT MAX(scanned_at) FROM scans WHERE shift_id = ?"), (shift_id,))
            times.append(cur.fetchone()[0])
            cur.execute(sql("SELECT MAX(created_at) FROM operation_events WHERE shift_id = ?"), (shift_id,))
            times.append(cur.fetchone()[0])
            for item in times:
                if item is None:
                    continue
                checked = parse_iso(item)
                if checked is None or checked.utcoffset() is None or checked.astimezone(timezone.utc) > actual:
                    return jsonify(error="После выбранного времени есть зарегистрированные операции. Проверь историю и укажи более позднее время."), 409

            cur.execute(sql("UPDATE activities SET ended_at = ? WHERE shift_id = ? AND ended_at IS NULL"), (end_iso, shift_id))
            cur.execute(sql("UPDATE shifts SET ended_at = ? WHERE id = ? AND ended_at IS NULL"), (end_iso, shift_id))
            if cur.rowcount != 1:
                conn.rollback()
                return jsonify(error="Смена уже закрыта другим запросом"), 409
            recorded = now_iso()
            cur.execute(sql("""
                INSERT INTO shift_admin_actions
                (id, shift_id, employee_id, admin_name, actual_ended_at, reason, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (str(uuid.uuid4()), shift_id, shift[0], admin_name, end_iso, reason, recorded))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return jsonify(ok=True, shift_id=shift_id, actual_ended_at=end_iso, recorded_at=recorded)


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


def _thermal_badge_qr(code):
    """The existing issued badge payload remains unchanged; only print format differs."""
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=12, border=4)
    qr.add_data(code)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _thermal_badge_page(rows):
    """One 58x40 mm page per active badge, same physical format as QR cell labels."""
    from html import escape
    cards=[]
    for employee_id,name,number,code in rows:
        if not code:
            # New print requests must never generate an unissued credential.
            continue
        png=_thermal_badge_qr(code)
        cards.append(
            '<div class="sheet"><div class="qr-zone"><img class="qr" alt="QR бейджа" '
            'src="data:image/png;base64,' + png + '"></div>'
            '<div class="meta"><div class="brand">GRIT WMS</div>'
            '<div class="employee">' + escape(str(name),quote=True) + '</div>'
            '<div class="badge-no">№' + escape(str(number),quote=True) + '</div>'
            '</div></div>'
        )
    if not cards:
        return Response("Активных бейджей с действующим QR нет",status=404,
                        mimetype="text/plain")
    content=''.join(cards)
    page="""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e">
<title>GRIT WMS · Печать бейджа · 58×40 мм</title>
<style>
@page {size:58mm 40mm; margin:0}
*{box-sizing:border-box}
:root{color-scheme:dark}
html,body{margin:0;padding:0;font:14px/1.3 Arial,Helvetica,sans-serif}
body{background:#09121e;color:#f1f6ff}
.tools{padding:14px;text-align:center}
.tools button{padding:11px 19px;background:#176eae;color:#fff;border:1px solid #358acc;border-radius:9px;cursor:pointer;font-weight:700}
.tools small{display:block;margin-top:8px;color:#9eb5ca}
.sheet{width:58mm;height:40mm;margin:12px auto;background:white;color:black;
 display:flex;align-items:center;justify-content:center;overflow:hidden;padding:1.5mm;
 break-after:page;page-break-after:always}
.sheet:last-of-type{break-after:auto;page-break-after:auto}
.qr-zone{width:31mm;height:33mm;flex:none;display:flex;justify-content:center;align-items:center}
.qr{width:30mm;height:30mm;display:block;image-rendering:pixelated;object-fit:contain}
.meta{width:23mm;min-width:0;height:34mm;display:flex;flex-direction:column;
 align-items:center;justify-content:center;text-align:center;gap:2mm;color:#000}
.brand{font-size:7pt;font-weight:700;letter-spacing:.2pt}
.employee{font-size:11pt;font-weight:800;line-height:1.12;overflow-wrap:anywhere;word-break:normal;
 max-width:100%;max-height:19mm;overflow:hidden}
.badge-no{font-size:12pt;font-weight:800}
@media print{
 :root{color-scheme:light}
 html,body{width:58mm;background:#fff!important;color:#000!important;margin:0;padding:0}
 .tools{display:none!important}
 .sheet{margin:0;width:58mm;height:40mm;background:#fff!important;color:#000!important;border:0!important;
  box-shadow:none!important;break-inside:avoid}
 .sheet:last-of-type{break-after:auto;page-break-after:auto}
}
</style></head><body>
<div class="tools"><button type="button" onclick="window.print()">Печать · 58×40 мм</button>
<small>Принтер: 58×40 мм, масштаб 100%, поля 0, без колонтитулов.</small></div>
""" + content + "</body></html>"
    response=Response(page,mimetype="text/html")
    response.headers["Cache-Control"]="no-store"
    return response


@wms_bp.get("/manager/badges/label")
def admin_print_one_badge():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    employee_id=str(request.args.get("id") or "").strip()
    if not employee_id:
        return Response("Укажи сотрудника",status=400,mimetype="text/plain")
    row=query_one("""SELECT id,name,badge_number,badge_code FROM employees
        WHERE id=? AND active=1""",(employee_id,))
    if not row:
        return Response("Активный бейдж не найден",status=404,mimetype="text/plain")
    return _thermal_badge_page([row])


@wms_bp.get("/manager/badges/labels/all")
def admin_print_all_badges():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    rows=query_all("""SELECT id,name,badge_number,badge_code FROM employees
        WHERE active=1 ORDER BY badge_number,name LIMIT 501""")
    if len(rows)>500:
        return Response("Слишком много бейджей за одну печать",status=413,
                        mimetype="text/plain")
    return _thermal_badge_page(rows)


@wms_bp.get("/api/admin/badges")
def admin_badges():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403

    import qrcode

    rows = query_all(
        "SELECT id, name, badge_number, badge_code FROM employees WHERE active = 1 ORDER BY badge_number, name"
    )
    archived_rows = query_all(
        "SELECT id,name,badge_number FROM employees WHERE active=0 ORDER BY badge_number,name"
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

    return jsonify(employees=employees, archived=[
        {"id": x[0], "name": x[1], "badge_number": x[2]} for x in archived_rows
    ])


def _badge_audit_table(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS employee_badge_audit (
        id TEXT PRIMARY KEY,
        employee_id TEXT NOT NULL,
        action TEXT NOT NULL,
        old_name TEXT,
        new_name TEXT,
        recorded_at TEXT NOT NULL
    )""")


def _badge_audit(cur, employee_id, action, old_name=None, new_name=None):
    cur.execute(sql("""INSERT INTO employee_badge_audit
        (id,employee_id,action,old_name,new_name,recorded_at)
        VALUES (?,?,?,?,?,?)"""),
        (str(uuid.uuid4()),employee_id,action,old_name,new_name,now_iso()))


def _badge_valid_name(value):
    name = str(value or "").strip()
    if not 2 <= len(name) <= 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return None
    return name


@wms_bp.post("/api/admin/badges")
def admin_create_badge():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    data = request.get_json(silent=True) or {}
    name = _badge_valid_name(data.get("name"))
    if not name:
        return jsonify(error="Имя сотрудника: от 2 до 80 символов, без управляющих символов"), 400
    employee_id = str(uuid.uuid4())
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if DATABASE_URL:
                # Serialize number assignment across API processes.
                cur.execute("SELECT pg_advisory_xact_lock(731924105)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            _badge_audit_table(cur)
            cur.execute("SELECT name FROM employees WHERE active=1")
            if any(str(r[0]).casefold() == name.casefold() for r in cur.fetchall()):
                return jsonify(error="Активный сотрудник с таким именем уже существует"), 409
            cur.execute("SELECT COALESCE(MAX(badge_number),0) FROM employees")
            badge_number=int(cur.fetchone()[0] or 0)+1
            cur.execute(sql("""INSERT INTO employees
                (id,name,rate,target,active,role,badge_number,badge_code,scanner_code)
                VALUES (?,?,?,?,1,'staff',?,?,?)"""),
                (employee_id,name,RATE,TARGET,badge_number,
                 make_badge_code(employee_id),make_badge_scanner_code(employee_id)))
            _badge_audit(cur,employee_id,"create",None,name)
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось создать сотрудника"), 503
        finally:
            cur.close()
    return jsonify(ok=True,id=employee_id,badge_number=badge_number), 201


@wms_bp.patch("/api/admin/badges/<employee_id>")
def admin_rename_badge(employee_id):
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    name = _badge_valid_name((request.get_json(silent=True) or {}).get("name"))
    if not name:
        return jsonify(error="Имя сотрудника: от 2 до 80 символов, без управляющих символов"), 400
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if DATABASE_URL:
                cur.execute("SELECT pg_advisory_xact_lock(731924105)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            _badge_audit_table(cur)
            cur.execute(sql("SELECT name FROM employees WHERE id=? AND active=1"),(employee_id,))
            old=cur.fetchone()
            if not old:
                return jsonify(error="Активный сотрудник не найден"), 404
            cur.execute(sql("SELECT name FROM employees WHERE active=1 AND id<>?"),(employee_id,))
            if any(str(r[0]).casefold() == name.casefold() for r in cur.fetchall()):
                return jsonify(error="Активный сотрудник с таким именем уже существует"), 409
            if old[0] != name:
                cur.execute(sql("UPDATE employees SET name=? WHERE id=? AND active=1"),(name,employee_id))
                if cur.rowcount != 1:
                    conn.rollback()
                    return jsonify(error="Изменение имени не сохранилось. Обнови страницу."), 409
                _badge_audit(cur,employee_id,"rename",old[0],name)
            cur.execute(sql("SELECT name FROM employees WHERE id=? AND active=1"),(employee_id,))
            persisted_name = cur.fetchone()
            if not persisted_name or persisted_name[0] != name:
                conn.rollback()
                return jsonify(error="Сервер не подтвердил сохранение имени"), 409
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось переименовать сотрудника"), 503
        finally:
            cur.close()
    return jsonify(ok=True,id=employee_id,name=persisted_name[0])


@wms_bp.post("/api/admin/badges/<employee_id>/archive")
def admin_archive_badge(employee_id):
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if DATABASE_URL:
                cur.execute("SELECT pg_advisory_xact_lock(731924105)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            _badge_audit_table(cur)
            cur.execute(sql("SELECT name,role,active FROM employees WHERE id=?"),(employee_id,))
            row=cur.fetchone()
            if not row:
                return jsonify(error="Сотрудник не найден"), 404
            if row[1] == "manager":
                return jsonify(error="Бейдж руководителя архивировать нельзя"), 403
            if not row[2]:
                conn.commit()
                return jsonify(ok=True,already_archived=True)
            cur.execute(sql("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL LIMIT 1"),(employee_id,))
            if cur.fetchone():
                return jsonify(error="У сотрудника открыта смена. Сначала закрой её через кабинет руководителя."), 409
            cur.execute(sql("UPDATE employees SET active=0 WHERE id=? AND active=1"),(employee_id,))
            _badge_audit(cur,employee_id,"archive",row[0],None)
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось архивировать бейдж"), 503
        finally:
            cur.close()
    return jsonify(ok=True)


@wms_bp.post("/api/admin/badges/<employee_id>/restore")
def admin_restore_badge(employee_id):
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    with db_connect() as conn:
        cur=conn.cursor()
        try:
            if DATABASE_URL:
                cur.execute("SELECT pg_advisory_xact_lock(731924105)")
            else:
                cur.execute("BEGIN IMMEDIATE")
            _badge_audit_table(cur)
            cur.execute(sql("SELECT name,active FROM employees WHERE id=?"),(employee_id,))
            row=cur.fetchone()
            if not row:
                return jsonify(error="Сотрудник не найден"), 404
            if row[1]:
                conn.commit()
                return jsonify(ok=True,already_active=True)
            cur.execute("SELECT name FROM employees WHERE active=1")
            if any(str(r[0]).casefold() == str(row[0]).casefold() for r in cur.fetchall()):
                return jsonify(error="Такое имя уже занято: сначала переименуй активного сотрудника"), 409
            cur.execute(sql("UPDATE employees SET active=1 WHERE id=? AND active=0"),(employee_id,))
            _badge_audit(cur,employee_id,"restore",None,row[0])
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось восстановить бейдж"), 503
        finally:
            cur.close()
    return jsonify(ok=True)


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


def _wb_hint_today():
    """WB order day for worker hints; Moscow midnight is the boundary."""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat()


@wms_bp.get("/api/fbs/wb-unscanned-hints")
def fbs_wb_unscanned_hints():
    """Read-only, shared WB queue hints for the authenticated employee shift.

    Not a personal picking plan: concurrent employees share this remainder.
    Only a full WB sticker scan can complete an order.
    """
    ensure_db()
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    if os.getenv("WB_FBS_V11_ENABLED", "0") != "1":
        return jsonify(available=False, reason="WB FBS API выключен")
    shift_id = str(request.args.get("shift_id") or "").strip()
    barcode = str(request.args.get("barcode") or "").strip()
    if not shift_id or not barcode or len(barcode) > 128:
        return jsonify(error="Нужны смена и ШК товара"), 400
    shift = query_one(
        "SELECT employee_id,ended_at FROM shifts WHERE id=?", (shift_id,))
    if not shift or shift[0] != employee["id"] or shift[1] is not None:
        return jsonify(error="Нет активной смены сотрудника"), 403
    account = os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"
    # Reuse the exact Moscow WB order creation-day rule from the manager's
    # existing unpicked-by-day page; no new ledger or date assumptions.
    from wb_unpicked_manager import _order_day
    today = _wb_hint_today()
    rows = query_all("""SELECT o.order_id,t.part_a,t.part_b,o.seller_article,o.created_at
        FROM wb_fbs_picking_queue q
        JOIN wb_fbs_orders o
          ON o.account_id=q.account_id AND o.order_id=q.order_id
        JOIN wb_fbs_order_skus s
          ON s.account_id=o.account_id AND s.order_id=o.order_id
        LEFT JOIN wb_fbs_stickers t
          ON t.account_id=o.account_id AND t.order_id=o.order_id
        WHERE q.account_id=?
          AND s.sku=?
          AND o.local_status='pending'
          AND o.wb_status IN ('confirm','complete')
        ORDER BY q.accepted_at DESC,o.order_id DESC""",
        (account, barcode))
    # Filter BEFORE limiting the returned hints: yesterday's queued orders
    # must not occupy today's first 100 rows. Unknown dates are excluded.
    rows = [row for row in rows if _order_day(row[4]) == today]
    total = len(rows)
    stickers = [
        {"part_a": str(row[1]).strip(), "part_b": str(row[2]).strip()}
        for row in rows[:100]
        if row[1] is not None and row[2] is not None
        and str(row[1]).strip().isdigit()
        and len(str(row[2]).strip()) == 4 and str(row[2]).strip().isdigit()
    ]
    return jsonify(available=True, shared=True, barcode=barcode,
                   day=today, remaining=total, truncated=total>100,
                   stickers=stickers[:12],
                   article=next((str(row[3]).strip() for row in rows if row[3]), ""),
                   note="Только заказы с датой создания WB сегодня по Москве. Старые дни в истории руководителя.")


@wms_bp.get("/api/manager/wb-fbs/picking-queue")
def manager_wb_fbs_picking_queue():
    """Live remaining WB orders from GRIT's admitted, unassembled queue."""
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if os.getenv("WB_FBS_V11_ENABLED", "0") != "1":
        return jsonify(state="disabled"), 503
    account = os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"
    day = (request.args.get("day") or "").strip()
    if day and day != _wb_hint_today():
        return jsonify(error="Доступна текущая дата по Москве"), 400
    try:
        if day:
            from wb_unpicked_manager import _order_day
            rows = query_all("""SELECT o.wb_status,o.created_at
                FROM wb_fbs_picking_queue q
                JOIN wb_fbs_orders o
                  ON o.account_id=q.account_id AND o.order_id=q.order_id
                WHERE q.account_id=?
                  AND o.local_status='pending'
                  AND o.wb_status IN ('confirm','complete')""", (account,))
            today_rows = [row for row in rows if _order_day(row[1]) == day]
            confirm = sum(1 for row in today_rows if row[0] == "confirm")
            delivery = sum(1 for row in today_rows if row[0] == "complete")
            remaining = len(today_rows)
        else:
            row = query_one(
                """SELECT
                    COUNT(*) FILTER (WHERE o.wb_status = 'confirm'),
                    COUNT(*) FILTER (WHERE o.wb_status = 'complete'),
                    COUNT(*)
                FROM wb_fbs_picking_queue q
                JOIN wb_fbs_orders o
                  ON o.account_id=q.account_id AND o.order_id=q.order_id
                WHERE q.account_id=?
                  AND o.local_status='pending'
                  AND o.wb_status IN ('confirm','complete')""",
                (account,),
            )
            confirm, delivery, remaining = (int(x or 0) for x in (row or (0, 0, 0)))
    except Exception:
        return jsonify(state="error", error="Очередь WB недоступна"), 503
    response = jsonify(
        state="connected", remaining_orders=remaining,
        confirm_orders=confirm, delivery_orders=delivery,
        **({"day": day} if day else {}),
    )
    response.headers["Cache-Control"] = "no-store"
    return response


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
        "name": "GRIT WMS · FBS терминал",
        "short_name": "GRIT FBS",
        "description": "Рабочий FBS-терминал GRIT WMS",
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


# Approved GRIT brand image. Keep the same source for browser and mobile icons.
from pathlib import Path
from functools import lru_cache
from PIL import Image

_GRIT_LOGO_PATH = Path(__file__).with_name("grit-logo.png")


@lru_cache(maxsize=3)
def _grit_icon_png(size):
    with Image.open(_GRIT_LOGO_PATH) as original:
        image = original.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True)
        return output.getvalue()


@wms_bp.get("/grit-logo.png")
def grit_logo():
    response = Response(_GRIT_LOGO_PATH.read_bytes(), mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@wms_bp.get("/favicon.svg")
def favicon_svg():
    picture = base64.b64encode(_grit_icon_png(128)).decode("ascii")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128">'
           '<image width="128" height="128" href="data:image/png;base64,' + picture + '"/></svg>')
    return Response(svg, mimetype="image/svg+xml")


@wms_bp.get("/favicon.ico")
def favicon_ico():
    return Response(_grit_icon_png(64), mimetype="image/png")


@wms_bp.get("/pwa-icon-192.png")
def pwa_icon_192():
    return Response(_grit_icon_png(192), mimetype="image/png")


@wms_bp.get("/pwa-icon-512.png")
def pwa_icon_512():
    return Response(_grit_icon_png(512), mimetype="image/png")


@wms_bp.get("/sw.js")
def pwa_service_worker():
    # A network-only worker: phone browsers must not receive a failed
    # navigation from a cache that GRIT never populated. In particular the
    # old fetch handler did respondWith(fetch(...).catch(caches.match(...))).
    # When Render was waking up, caches.match() returned undefined, turning
    # temporary unavailability into Chrome ERR_FAILED / failed navigation.
    # Keep install/activate for the PWA; let Chrome and Render own requests.
    js = """
// GRIT mobile PWA v3. No fetch interception and no caching of WMS data.
self.addEventListener('install', event => {
  self.skipWaiting();
});
self.addEventListener('activate', event => {
  event.waitUntil(self.clients.claim());
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
<title>GRIT WMS · Перемещение</title>
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

/* GRIT WMS unified dark shell. Functional scanning/QR graphics retain contrast. */
:root{color-scheme:dark;--g-bg:#09121e;--g-surface:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-blue:#176eae;--g-orange:#ff9230}
html,body{background:var(--g-bg)!important;color:var(--g-text)!important}
body .card,body .panel,body .scanbox,body .stats,body .activeDoc,body #topbar{background:#142337!important;color:var(--g-text)!important;border-color:var(--g-line)!important;box-shadow:none!important}
body .muted,body .small,body .meta{color:var(--g-muted)!important}
body h1,body h2,body h3{color:#f3f8fd!important}
body a{color:#85caff}
body input,body select,body textarea{background:#0d1d2c!important;color:#ecf5ff!important;border:1px solid #42607a!important;border-radius:9px}
body input::placeholder{color:#92aec3}
body button:not(.danger){background:#176eae;color:#fff;border:1px solid #358acc}
body button.secondary,body .nav a,body a.secondary{background:#233b50!important;color:#e0f0ff!important;border:1px solid #375a75!important}
body .nav a.active{background:#176eae!important;color:#fff!important}
body .warning,body .note{background:#35291d!important;color:#ffe0ad!important;border:1px solid #805b34!important}
body .err,body .bad{background:#40272e!important;color:#ff9da1!important}
body .ok{background:#173e36!important;color:#81e9b9!important}
button:focus-visible,input:focus-visible,a:focus-visible,select:focus-visible{outline:2px solid var(--g-orange)!important;outline-offset:2px}

:root{--bg:#09121e;--panel:#142337;--panel2:#192e43;--text:#f1f6ff;--muted:#9eb5ca;--line:#30465c;--accent:#176eae;--accent2:#233b50}
body .nav a{color:#e0f0ff!important}
</style>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head>
<body>
<div class="wrap">
  <div class="nav">
    <a href="/">📦 Приёмка</a>
    <a href="/fbs">🧺 Сборка</a>
    <a class="active" href="/move">↔ Перемещение</a>
    <a href="/replenishment">📦 Пополнение <span id="repCountMove"></span></a>
  </div>
  <script>
  (function(){
   const n=document.getElementById('repCountMove');
   async function update(){
    if(document.hidden)return;
    try{
     const r=await fetch('/api/replenishment/tasks',{cache:'no-store'});
     if(!r.ok)return;
     const d=await r.json();
     const count=(d.tasks||[]).filter(t=>t.status==='new'||t.status==='started').length;
     n.textContent=count?'('+count+')':'';
    }catch(e){}
   }
   update();setInterval(update,30000);
  })();
  </script>
  <div id="topbar">
    <div class="who"><span id="identity"></span></div>
    <a href="/attendance/exit-scan">🚪 Завершить смену</a>
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


def pdf_picking_active(shift_id, employee_id):
    """Automatically enforce PDF checking for active shifts when PDFs exist.

    Employees have no toggle. Legacy pdf_fbs_shift_modes values are ignored:
    an old disabled state must not silently disable mispick protection.
    No PDF orders means legacy FBS picking remains available.
    """
    if not shift_id or not employee_id:
        return False
    valid_shift = query_one(
        "SELECT 1 FROM shifts WHERE id=? AND employee_id=? AND ended_at IS NULL",
        (shift_id, employee_id),
    )
    return bool(valid_shift and query_one("SELECT 1 FROM pdf_fbs_orders LIMIT 1"))


@wms_bp.get("/manager/fbs-skip")
def manager_fbs_skip_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    from fbs_skip_authorization import MANAGER_SKIP_PAGE
    return Response(MANAGER_SKIP_PAGE, headers={"Cache-Control": "no-store"}, mimetype="text/html")


@wms_bp.post("/api/manager/fbs/skip-qr")
def manager_create_fbs_skip_qr():
    ensure_db()
    if not manager_access():
        return jsonify(error="Только руководитель может создать QR"), 403
    from fbs_skip_authorization import issue_skip_code, qr_png_data_url
    code, expires_at = issue_skip_code()
    response = jsonify(ok=True, image=qr_png_data_url(code), expires_at=expires_at)
    response.headers["Cache-Control"] = "no-store"
    return response


@wms_bp.get("/api/fbs/pending")
def fbs_pending_api():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Требуется вход по бейджу"), 401
    shift_id = str(request.args.get("shift_id") or "").strip()
    valid = query_one(
        "SELECT 1 FROM shifts WHERE id=? AND employee_id=? AND ended_at IS NULL",
        (shift_id, emp["id"]),
    )
    if not valid:
        return jsonify(error="Смена не принадлежит сотруднику"), 403
    from fbs_skip_authorization import pending_for_shift
    return jsonify(ok=True, pending=pending_for_shift(shift_id, emp["id"]))


@wms_bp.post("/api/fbs/skip-item")
def fbs_skip_item():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Требуется вход по бейджу"), 401
    data = request.get_json(silent=True) or {}
    shift_id = str(data.get("shift_id") or "").strip()
    code = str(data.get("code") or "").strip()
    valid = query_one(
        "SELECT 1 FROM shifts WHERE id=? AND employee_id=? AND ended_at IS NULL",
        (shift_id, emp["id"]),
    )
    if not valid:
        return jsonify(error="Смена не принадлежит сотруднику"), 403
    active = current_activity(shift_id)
    if not active or active["type"] != "picking":
        return jsonify(error="Сейчас включена не сборка"), 409
    from fbs_skip_authorization import consume_skip_code
    try:
        item = consume_skip_code(code, shift_id, emp["id"])
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    return jsonify(ok=True, skipped=True, order_counted=False)


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

    # A revoked badge cannot create new scans. But a lost ACK for an already
    # committed request is safe to replay: same signed prior employee, bound
    # shift and immutable request_id, verified against the durable ledger.
    prior_employee_id = str(session.get("employee_id") or "").strip()
    prior_shift_id = str(session.get("employee_shift_id") or "").strip()
    employee = get_session_employee()
    if not employee:
        request_id = str(data.get("request_id") or "").strip()
        if prior_employee_id and prior_shift_id == shift_id and request_id:
            try:
                request_id = str(uuid.UUID(request_id))
                from fbs_transactions import replay
                from wb_qr_codec import canonical_wb_sticker
                replay_barcode = canonical_wb_sticker(barcode)
                if replay(request_id, shift_id, prior_employee_id, replay_barcode, product_barcode):
                    return jsonify(ok=True, replayed=True, **shift_stats(shift_id))
            except ValueError:
                pass
        return jsonify(error="Требуется вход по бейджу"), 401

    shift = query_one(
        "SELECT employee_id, ended_at FROM shifts WHERE id = ?",
        (shift_id,),
    )
    if not shift:
        return jsonify(error="Смена не найдена"), 404
    if shift[0] != employee["id"]:
        return jsonify(error="Эта смена принадлежит другому сотруднику"), 403

    request_id = str(data.get("request_id") or "").strip()
    if request_id:
        try:
            request_id = str(uuid.UUID(request_id))
        except ValueError:
            return jsonify(error="Неверный номер операции"), 400
    from fbs_transactions import replay, commit_scan
    if os.getenv("WB_FBS_V11_ENABLED", "0") != "1" and (request_id or pdf_picking_active(shift_id, employee["id"])):
        from wb_qr_codec import canonical_wb_sticker
        barcode = canonical_wb_sticker(barcode)

    # WB FBS v1.1 is opt-in. The product is already pending on this terminal;
    # the *second* scan identifies the WB order. No legacy article fallback.
    if os.getenv("WB_FBS_V11_ENABLED", "0") == "1":
        from wb_fbs_sync_v11 import verify_pair, log_verification, normalize_api_sticker
        barcode = normalize_api_sticker(barcode)
        from fbs_skip_authorization import pending_for_shift
        # Compare the exact machine-readable sticker value returned by WB API.
        # Do not infer an orderId by decoding the QR payload.
        account = os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"
        try:
            if replay(request_id, shift_id, employee["id"], barcode, product_barcode):
                return jsonify(ok=True, replayed=True, **shift_stats(shift_id))
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        pending = pending_for_shift(shift_id, employee["id"])
        if not pending or pending["barcode"] != product_barcode:
            return jsonify(error="Сначала отсканируй товар. Товар не подтверждён текущей сменой."), 409
        verdict = verify_pair(account, product_barcode, barcode)
        log_verification(account, employee["id"], shift_id,
                         product_barcode, barcode, verdict)
        if verdict["result"] != "MATCH":
            from fbs_skip_authorization import complete_pending_item
            complete_pending_item(shift_id, employee["id"], product_barcode)
            return jsonify(error="WB FBS: " + verdict["result"], reason=verdict["result"],
                           reset_pending=True), 409
        try:
            replayed = commit_scan(
                shift_id, employee["id"], barcode, product_barcode,
                article, product_name, verdict["order_id"], scan_type,
                request_id, True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            if query_one("SELECT id FROM scans WHERE barcode=?", (barcode,)):
                return jsonify(error="Этот заказ уже был засчитан"), 409
            raise
        return jsonify(ok=True, replayed=replayed, **shift_stats(shift_id))
    try:
        if replay(request_id, shift_id, employee["id"], barcode, product_barcode):
            return jsonify(ok=True, replayed=True, **shift_stats(shift_id))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409

    if request_id:
        try:
            replayed = commit_scan(shift_id, employee["id"], barcode, product_barcode,
                                   article, product_name, None, scan_type, request_id, True)
        except ValueError as exc:
            return jsonify(error=str(exc)), 409
        except Exception:
            if query_one("SELECT id FROM scans WHERE barcode=?", (barcode,)):
                return jsonify(error="Этот заказ уже был засчитан"), 409
            raise
        return jsonify(ok=True, replayed=replayed, **shift_stats(shift_id))

    if shift[1]:
        return jsonify(error="Смена уже завершена"), 409
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

    # Only normalize at the WB-sticker stage; the earlier item scan stays raw.
    # The printed Code128 is decimal, while the QR contains WB's Base64 form.
    pdf_control = pdf_picking_active(shift_id, employee["id"])
    from fbs_skip_authorization import pending_for_shift
    pending = pending_for_shift(shift_id, employee["id"])
    if pdf_control and (not pending or pending["barcode"] != product_barcode):
        return jsonify(
            error="Товар не подтверждён текущим сканированием. Проверь скан товара.",
            reason="pending_product_required",
        ), 409
    if pdf_control:
        from wb_qr_codec import canonical_wb_sticker
        barcode = canonical_wb_sticker(barcode)

    # A second scan of the same merchandise barcode is not evidence of a WB
    # order sticker. Reject it independently of optional WB API matching.
    # A genuine WB label that collides with a product code needs a separately
    # verified exception: two identical scanned strings are indistinguishable.
    if barcode == product_barcode:
        log_event(
            shift[0],
            "fbs",
            "wb_same_as_product",
            shift_id=shift_id,
            object_code=barcode,
            result="second_scan_equals_product",
            article=article or None,
            product_name=product_name or None,
        )
        return jsonify(
            error="❌ Повторный ШК товара вместо этикетки WB! Заказ НЕ засчитан. Сканируй стикер WB.",
            reason="wb_same_as_product",
        ), 409

    wb_order_id = None
    if pdf_control:
        pdf_expected = query_one(
            "SELECT article, product_barcode FROM pdf_fbs_orders WHERE wb_barcode=?",
            (barcode,),
        )
        if not pdf_expected:
            log_event(
                shift[0], "fbs", "pdf_order_missing", shift_id=shift_id,
                object_code=barcode, result="not-in-pdf",
                article=article or None, product_name=product_name or None,
            )
            return jsonify(
                error="❌ Стикер WB отсутствует в загруженном PDF. Заказ НЕ засчитан.",
                reason="pdf_order_missing",
            ), 409
        expected_article, expected_barcode = pdf_expected
        if product_barcode != expected_barcode:
            log_event(
                shift[0], "fbs", "product_mismatch", shift_id=shift_id,
                object_code=barcode,
                result="pdf_expected=" + expected_article + ";scanned=" + product_barcode,
                article=article or None, product_name=product_name or None,
            )
            return jsonify(
                error="❌ НЕ ТОТ ТОВАР. Для этого WB-заказа нужен: " + expected_article,
                reason="pdf_product_mismatch",
                expected_article=expected_article,
            ), 409
        wb_order_id = "pdf:" + barcode
    else:
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
        replayed = commit_scan(shift_id, employee["id"], barcode, product_barcode,
                               article, product_name, wb_order_id, scan_type,
                               request_id, bool(request_id or pdf_control or pending))
    except ValueError as exc:
        return jsonify(error=str(exc)), 409
    except Exception:
        # A concurrent scan from a different shift may win the global WB unique
        # index. It is a conflict, not a replay for this employee/request.
        duplicate = query_one("SELECT id FROM scans WHERE barcode=?", (barcode,))
        if duplicate:
            return jsonify(error="Этот заказ уже был засчитан"), 409
        raise
    return jsonify(ok=True, replayed=replayed, **shift_stats(shift_id))


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
    from fbs_transactions import close_shift
    close_shift(shift_id, shift[1])
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


def _ozon_fbo_units_for_shift(shift):
    """Read-only FBO units counted during this specific shift; never count as FBS orders."""
    try:
        rows = query_all(
            """SELECT COALESCE(SUM(quantity),0), COUNT(*)
               FROM ozon_fbo_pick_events
               WHERE employee_id=? AND voided_at IS NULL
                 AND created_at >= ? AND created_at < ?""",
            (shift["employee_id"], shift["started_at"],
             shift["ended_at"] or now_iso()),
        )
        return int(rows[0][0] or 0), int(rows[0][1] or 0)
    except Exception:
        # A new/unused FBO module may not have created its event table yet.
        return 0, 0


def _with_market_details(shift):
    units, operations = _ozon_fbo_units_for_shift(shift)
    shift["wb_fbs_orders"] = shift["orders"]
    shift["ozon_fbo_units"] = units
    shift["ozon_fbo_scans"] = operations
    shift["market_breakdown_available"] = True
    return shift


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
            workers.append(_with_market_details(item))
    completed = []
    date_from = request.args.get("from")
    date_to = request.args.get("to")
    if date_from or date_to:
        try:
            start = parse_iso(date_from)
            end = parse_iso(date_to)
            if not start or not end or not start.tzinfo or not end.tzinfo or end <= start:
                raise ValueError("invalid interval")
            if (end - start).total_seconds() > 31 * 86400:
                raise ValueError("interval too large")
        except (ValueError, TypeError, OverflowError):
            return jsonify(error="Неверный период статистики"), 400
        ended_rows = query_all(
            """
            SELECT s.id FROM shifts s
            WHERE s.ended_at IS NOT NULL AND s.ended_at >= ? AND s.ended_at < ?
            ORDER BY s.ended_at DESC LIMIT 100
            """,
            (start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()),
        )
        for row in ended_rows:
            item = shift_stats(row[0])
            if item:
                completed.append(_with_market_details(item))
    return jsonify(workers=workers, completed=completed, target=TARGET, rate=RATE)
