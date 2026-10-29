import os
import json
import uuid
import math
import time
import threading
import tempfile
from datetime import timedelta, datetime, timezone
import sqlite3
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, request, jsonify, session, redirect, Response, after_this_request
from flask.sessions import SecureCookieSessionInterface
from wms import (
    wms_bp,
    init_wms_db,
    get_session_employee,
    log_event,
    query_one,
    query_all,
    db_connect,
    execute,
    now_iso,
    manager_access,
    WB_STRICT_MATCHING,
)
from partner_pallets import partner_bp, resolve_receiving_pallet, claim_receiving_pallet, set_receiving_pallet_status
from replenishment import replenishment_bp
from replenishment_checks import replenishment_checks_bp
from warehouse_map import warehouse_map_bp
from cell_audit import cell_audit_bp
from fbs_diagnostics import diag_bp
from shift_history import shift_history_bp
from fbs_pdf_import import fbs_pdf_bp
from onix_print_bridge import print_bridge_bp
from ozon_fbo import ozon_fbo_bp
from ozon_fbo_picking import fbo_picking_bp
from receiving_idempotency import (
    init_receiving_operations,
    claim_operation,
    read_operation,
    finish_operation,
    mark_uncertain,
    reject_operation,
    has_unresolved_operations,
    reconcile_after_manual_check,
)
from fbs_catalog import (
    init_fbs_catalog_db,
    find_persisted_fbs_barcode,
    save_fbs_catalog_snapshot,
    fbs_catalog_status,
)
from storage_backup import (
    restore_if_needed,
    start_worker as start_backup_worker,
    backup_now,
    status as backup_status,
    RestoreRequiredError,
    download_backup_for_migration,
)

app = Flask(__name__)
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required in the environment")
app.secret_key = SECRET_KEY

class ONIXScopedSessionInterface(SecureCookieSessionInterface):
    """Same Flask service and database, but independent partner browser cookie.

    A partner login/logout must never clear a manager or employee session.
    All partner pages and their catalog API are served under /partners.
    Manager administration (/manager/partners) retains the manager cookie.
    """
    def get_cookie_name(self, app):
        if request.path == "/partners" or request.path.startswith("/partners/"):
            return "onix_partner_session"
        return super().get_cookie_name(app)

    def get_expiration_time(self, app, session):
        # Manager logins persist across browser restarts for at most 7 days.
        # Employee TSD sessions keep their existing separate policy.
        if session.permanent and session.get("manager_web") is True:
            return datetime.now(timezone.utc) + timedelta(days=7)
        return super().get_expiration_time(app, session)

app.session_interface = ONIXScopedSessionInterface()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Rolling persistent employee cookie: closing Safari/PWA must never log out a TSD.
    # The official employee logout still clears the cookie after supervisor QR.
    PERMANENT_SESSION_LIFETIME=timedelta(days=3650),
    SESSION_REFRESH_EACH_REQUEST=True,
)
app.register_blueprint(wms_bp)
app.register_blueprint(partner_bp)
app.register_blueprint(replenishment_bp)
app.register_blueprint(replenishment_checks_bp)
app.register_blueprint(warehouse_map_bp)
app.register_blueprint(cell_audit_bp)
app.register_blueprint(diag_bp)
app.register_blueprint(shift_history_bp)
app.register_blueprint(fbs_pdf_bp)
app.register_blueprint(print_bridge_bp)
app.register_blueprint(ozon_fbo_bp)
app.register_blueprint(fbo_picking_bp)

_WMS_READY = threading.Event()
_WMS_INITIALIZER_STARTED = False
_WMS_STARTUP_LOCK = threading.Lock()
_WMS_STARTUP_STATE = {
    "attempts": 0,
    "last_attempt_at": None,
    "last_error": "",
    "stage": "boot",
}

def _build_id():
    return (
        os.getenv("RENDER_GIT_COMMIT", "").strip()
        or os.getenv("GIT_COMMIT", "").strip()
        or "unknown"
    )

def _startup_snapshot():
    with _WMS_STARTUP_LOCK:
        return dict(_WMS_STARTUP_STATE)

def _set_startup_stage(stage):
    with _WMS_STARTUP_LOCK:
        _WMS_STARTUP_STATE["stage"] = str(stage)
    print(f"WMS startup stage: {stage}", flush=True)

def _startup_public_message():
    state = _startup_snapshot()
    raw = str(state.get("last_error") or "").lower()
    if not raw:
        return "Подключаю рабочую базу данных."
    if "accessdenied" in raw or "403" in raw or "forbidden" in raw:
        return "Нет доступа к резервной копии в Yandex Object Storage."
    if "nosuchkey" in raw or "not found" in raw or "404" in raw or "no remote sqlite backup" in raw:
        return "Резервная копия базы не найдена в Yandex Object Storage."
    if "integrity_check" in raw or "malformed" in raw or "corrupt" in raw:
        return "Резервная копия базы повреждена и не была подключена."
    if "timeout" in raw or "timed out" in raw or "connection" in raw or "endpoint" in raw:
        return "Нет стабильной связи с Yandex Object Storage. Повторяю подключение."
    return "Не удалось подключить рабочую базу. ONIX автоматически повторяет попытку."

def _migrate_sqlite_backup_to_postgres_if_needed():
    if not os.getenv("DATABASE_URL", "").strip():
        return
    if os.getenv("WMS_MIGRATE_SQLITE_FROM_S3", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return

    # Only migrate into a fresh Postgres. Never merge blindly into live data.
    business_tables = (
        "employees", "shifts", "scans", "activities",
        "operation_events", "external_pallets", "wb_orders", "daily_targets",
    )
    counts = {}
    for table in business_tables:
        try:
            row = query_one(f"SELECT COUNT(*) FROM {table}")
            counts[table] = int(row[0] if row else 0)
        except Exception:
            counts[table] = 0

    non_employee_rows = sum(v for k, v in counts.items() if k != "employees")
    fresh_seed_only = False
    if non_employee_rows == 0 and counts.get("employees", 0) <= 4:
        rows = query_all("SELECT id FROM employees ORDER BY id")
        actual_ids = {str(row[0]) for row in rows}
        seed_ids = {"viktor", "anton", "ilya", "loader"}
        fresh_seed_only = actual_ids.issubset(seed_ids)

    if not fresh_seed_only and sum(counts.values()):
        print(f"SQLite->Postgres migration skipped: target already has business data {counts}")
        return

    fd, source_path = tempfile.mkstemp(prefix="onix-migrate-", suffix=".db")
    os.close(fd)
    try:
        _set_startup_stage("download_backup")
        key = download_backup_for_migration(source_path)
        _set_startup_stage("open_sqlite")
        src = sqlite3.connect(source_path)
        try:
            _set_startup_stage("integrity_check")
            check = src.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError("Migration source SQLite integrity_check failed")

            total_inserted = 0
            for table in business_tables:
                _set_startup_stage(f"import:{table}")
                src_cols = [row[1] for row in src.execute(f"PRAGMA table_info({table})").fetchall()]
                if not src_cols:
                    continue
                rows = src.execute(
                    "SELECT " + ",".join('"' + col.replace('"','""') + '"' for col in src_cols) +
                    f' FROM "{table}"'
                ).fetchall()
                if not rows:
                    continue

                placeholders = ",".join(["%s"] * len(src_cols))
                cols_sql = ",".join('"' + col.replace('"','""') + '"' for col in src_cols)
                stmt = f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING'
                with db_connect() as conn:
                    cur = conn.cursor()
                    cur.executemany(stmt, rows)
                    conn.commit()
                    cur.close()
                total_inserted += len(rows)

            # Verify source row counts are present in Postgres.
            _set_startup_stage("verify")
            for table in business_tables:
                src_count = int(src.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                dst_row = query_one(f'SELECT COUNT(*) FROM "{table}"')
                dst_count = int(dst_row[0] if dst_row else 0)
                if dst_count < src_count:
                    raise RuntimeError(
                        f"Migration verification failed for {table}: source={src_count}, target={dst_count}"
                    )
            print(f"SQLite->Postgres migration complete from {key}: {total_inserted} source rows processed", flush=True)
            _set_startup_stage("migration_complete")
        finally:
            src.close()
    finally:
        try:
            os.remove(source_path)
        except OSError:
            pass


def _wms_initializer():
    while not _WMS_READY.is_set():
        with _WMS_STARTUP_LOCK:
            _WMS_STARTUP_STATE["attempts"] += 1
            _WMS_STARTUP_STATE["last_attempt_at"] = now_iso()
        try:
            _set_startup_stage("restore_check")
            restore_if_needed()
            _set_startup_stage("init_db")
            init_wms_db()
            init_fbs_catalog_db()
            init_receiving_operations()
            _set_startup_stage("migration")
            _migrate_sqlite_backup_to_postgres_if_needed()
            _set_startup_stage("backup_worker")
            start_backup_worker()
            with _WMS_STARTUP_LOCK:
                _WMS_STARTUP_STATE["last_error"] = ""
                _WMS_STARTUP_STATE["stage"] = "ready"
            _WMS_READY.set()
            print("WMS startup stage: ready", flush=True)
            # Build/refresh the permanent FBS catalog in the background after
            # database startup. It must not block workers, login or picking.
            if TOKEN:
                start_pack_warmup()
            return
        except Exception as exc:
            with _WMS_STARTUP_LOCK:
                _WMS_STARTUP_STATE["last_error"] = str(exc)
            print(f"WMS startup retry: {exc}", flush=True)
            time.sleep(10)

def _start_wms_initializer():
    global _WMS_INITIALIZER_STARTED
    with _WMS_STARTUP_LOCK:
        if _WMS_INITIALIZER_STARTED:
            return
        _WMS_INITIALIZER_STARTED = True
    thread = threading.Thread(target=_wms_initializer, name="wms-startup", daemon=True)
    thread.start()

@app.before_request
def require_employee_login():
    # Gunicorn workers are forked processes. Start the initializer inside the
    # serving worker so its in-memory readiness flag belongs to this process.
    _start_wms_initializer()
    path = request.path

    # Enforce the manager's absolute 7-day login age even if the client
    # retains or manually replays a signed cookie after browser expiry.
    if session.get("manager_web") is True and session.get("manager_login_at") is not None:
        try:
            age = time.time() - float(session["manager_login_at"])
        except (ValueError, TypeError):
            age = float("inf")
        if age < -300 or age > 7 * 86400:
            session.pop("manager_web", None)
            session.pop("manager_login_at", None)

    # Liveness must stay reachable so Render keeps the process alive while
    # Object Storage restore retries in the background.
    if path in ("/api/health", "/api/startup", "/favicon.svg", "/favicon.ico"):
        return None

    # Do not let any route touch SQLite until restore has completed. This
    # prevents Flask/health/login code from accidentally creating an empty DB.
    if not _WMS_READY.is_set():
        if path.startswith("/api/"):
            return jsonify(
                error="ONIX WMS восстанавливает базу данных",
                startup=_startup_snapshot(),
            ), 503
        return Response(
            """<!doctype html><html lang="ru"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>ONIX WMS · запуск</title>
            <body style="font-family:system-ui;background:#e9eef3;color:#25313d;display:grid;place-items:center;min-height:100vh;margin:0">
            <div style="max-width:520px;background:#fff;padding:24px;border-radius:16px;border:1px solid #d7e0e7">
            <h2 style="margin-top:0">ONIX WMS запускается</h2>
            <p>Восстанавливаю рабочую базу данных. Страница обновится автоматически.</p>
            <p style="color:#6b7785">""" + _startup_public_message() + """</p>
            <p style="color:#8a5a12;font-size:13px">Попытка №""" + str(_startup_snapshot().get("attempts", 0)) + """ · этап: """ + str(_startup_snapshot().get("stage", "unknown")) + """</p>
            <p style="color:#8b96a3;font-size:12px">Сборка: """ + _build_id()[:12] + """</p>
            <script>setTimeout(()=>location.reload(),5000)</script>
            </div></body></html>""",
            status=503,
            mimetype="text/html",
        )

    public_paths = {
        "/login",
        "/api/auth/login",
        "/api/health",
        "/supervisor-terminal",
        "/api/supervisor-terminal/qr",
        "/manager-login",
        "/api/manager/login",
    }
    if path in public_paths or path == "/partners/login" or path.startswith("/static/"):
        return None

    # Partner routes use their own scoped session; no employee badge required.
    # Authentication and CSRF are enforced by the partner blueprint.
    if path.startswith("/partners/") or path == "/partners":
        return None

    # The opt-in test-print agent authenticates with its own bearer token
    # inside onix_print_bridge; it never receives an employee session.
    if path in ("/api/print-agent/test/next", "/api/print-agent/test/ack"):
        return None

    # Отдельная браузерная сессия руководителя для ПК.
    # Она даёт доступ только к desktop-панели и её API, без имитации сотрудника/TSD.
    manager_paths = {
        "/manager/partners",
        "/api/partners/accounts",
        "/manager",
        "/manager/shifts",
        "/manager/shift-history",
        "/manager/fbs-pdf",
        "/manager/onix-print-test",
        "/api/manager/fbs-pdf/preview",
        "/api/manager/shift-history",
        "/api/manager/shifts/actions",
        "/api/manager/shifts/force-close",
        "/manager/replenishment",
        "/manager/warehouse-map",
        "/manager/cell-audit",
        "/api/manager/cell-audit/locate",
        "/api/manager/cell-audit/preview",
        "/api/manager/cell-audit/assign",
        "/api/manager/cell-audit/assignments",
        "/api/manager/warehouse-map",
        "/api/manager/warehouse-map/locations",
        "/api/manager/warehouse-map/location/manage",
        "/manager/fbs-diagnostics",
        "/api/manager/fbs-diagnostics",
        "/api/manager/replenishment/tasks",
        "/api/manager/replenishment/create",
        "/api/manager/replenishment/action",
        "/api/manager/replenishment/catalog",
        "/api/manager/replenishment/locations",
        "/manager/replenishment/label",
        "/manager/replenishment/labels/all",
        "/manager/ozon-fbo",
        "/manager/ozon-fbo/picking",
        "/api/manager/ozon-fbo/preview",
        "/api/manager/ozon-fbo/import",
        "/api/manager/ozon-fbo/supplies",
        "/stats",
        "/fbs-journal",
        "/history",
        "/badges",
        "/api/manager/logout",
        "/api/fbs/dashboard",
        "/api/fbs/overview",
        "/api/fbs/journal",
        "/api/fbs/hourly",
        "/api/history",
        "/api/stats/employees",
        "/api/admin/badges",
        "/api/admin/supervisor-exit-code",
        "/receiving-monitor",
        "/api/receiving/overview",
        "/api/wb/status",
        "/api/wb/sync",
        "/api/manager/daily-plan",
        "/api/operations/deviations",
        "/api/system/status",
    }
    # Desktop manager routes are authenticated only by the dedicated
    # manager password/session. Do not fall through to employee badge auth.
    if path in manager_paths or path.startswith("/api/manager/ozon-fbo/"):
        if session.get("manager_web") is True:
            return None
        if path.startswith("/api/"):
            return jsonify(error="Требуется вход в ПК-панель"), 403
        return redirect("/manager-login")

    employee = get_session_employee()
    if employee:
        return None
    if path.startswith("/api/"):
        return jsonify(error="Требуется вход по бейджу"), 401
    return redirect("/login")


API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()

WB_API_BASE = "https://marketplace-api.wildberries.ru"
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "").strip()
WB_SYNC_DAYS = max(1, min(30, int(os.getenv("WB_SYNC_DAYS", "5") or 5)))
WB_SYNC_LOCK = threading.Lock()
WB_SYNC_STATE = {
    "running": False,
    "last_error": "",
    "last_started_at": None,
    "last_finished_at": None,
    "orders_seen": 0,
    "stickers_saved": 0,
}

# Быстрый индекс палетных штрихкодов в памяти Render.
# После первого прогрева скан не перебирает каталог МойСклад заново.
PACK_CACHE = {}
PRODUCT_BARCODE_CACHE = {}
PRODUCT_LIST_CACHE = {"ts": 0.0, "rows": []}
DETAILED_PRODUCT_IDS = set()
CACHE_LOCK = threading.RLock()
WARMUP_STATE = {"running": False, "ready": False, "indexed": 0, "error": ""}
PRODUCT_LIST_TTL = 3600
_BOOTSTRAP_CACHE = {"data": None, "expires_at": 0.0}
_BOOTSTRAP_CACHE_LOCK = threading.Lock()
BOOTSTRAP_CACHE_SECONDS = max(60, int(os.getenv("BOOTSTRAP_CACHE_SECONDS", "300") or 300))

def _wb_headers():
    return {
        "Authorization": "Bearer " + WB_API_TOKEN,
        "Content-Type": "application/json",
    }


def _wb_order_product(order):
    product = order.get("product") if isinstance(order.get("product"), dict) else order
    article = str(product.get("article") or order.get("article") or "").strip()
    raw_skus = product.get("skus") or order.get("skus") or []
    skus = [str(x).strip() for x in raw_skus if str(x).strip()]
    nm_id = product.get("nmId") or order.get("nmId")
    chrt_id = product.get("chrtId") or order.get("chrtId")
    return article, skus, nm_id, chrt_id


def _save_wb_order(order, sticker):
    order_id = str(sticker.get("orderId") or order.get("id") or "").strip()
    wb_barcode = str(sticker.get("barcode") or "").strip()
    if not order_id or not wb_barcode:
        return False

    article, skus, nm_id, chrt_id = _wb_order_product(order)
    supply_id = str(order.get("supplyId") or "").strip()
    synced_at = now_iso()
    existing = query_one("SELECT order_id FROM wb_orders WHERE order_id = ?", (order_id,))
    values = (
        wb_barcode,
        article or None,
        json.dumps(skus, ensure_ascii=False),
        str(nm_id) if nm_id is not None else None,
        str(chrt_id) if chrt_id is not None else None,
        supply_id or None,
        synced_at,
        order_id,
    )
    if existing:
        execute(
            """
            UPDATE wb_orders
            SET wb_barcode = ?, article = ?, skus_json = ?, nm_id = ?, chrt_id = ?,
                supply_id = ?, synced_at = ?
            WHERE order_id = ?
            """,
            values,
        )
    else:
        execute(
            """
            INSERT INTO wb_orders
            (wb_barcode, article, skus_json, nm_id, chrt_id, supply_id, synced_at, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return True


def sync_wb_orders():
    if not WB_API_TOKEN:
        raise RuntimeError("WB_API_TOKEN не настроен")

    with WB_SYNC_LOCK:
        WB_SYNC_STATE.update({
            "running": True,
            "last_error": "",
            "last_started_at": now_iso(),
            "orders_seen": 0,
            "stickers_saved": 0,
        })
        try:
            now_ts = int(time.time())
            date_from = now_ts - WB_SYNC_DAYS * 86400
            cursor = 0
            orders = {}
            seen_cursors = set()

            while True:
                r = requests.get(
                    WB_API_BASE + "/api/v3/orders",
                    headers=_wb_headers(),
                    params={
                        "limit": 1000,
                        "next": cursor,
                        "dateFrom": date_from,
                        "dateTo": now_ts,
                    },
                    timeout=30,
                )
                r.raise_for_status()
                payload = r.json()
                rows = payload.get("orders", []) or []
                for order in rows:
                    order_id = order.get("id")
                    if order_id is not None:
                        orders[str(order_id)] = order

                next_cursor = payload.get("next")
                if not rows or next_cursor in (None, 0, "0") or next_cursor == cursor:
                    break
                if str(next_cursor) in seen_cursors:
                    break
                seen_cursors.add(str(next_cursor))
                cursor = next_cursor
                time.sleep(0.22)

            existing_rows = query_all("SELECT order_id FROM wb_orders WHERE wb_barcode IS NOT NULL AND wb_barcode <> ''")
            existing_ids = {str(x[0]) for x in existing_rows}

            # Стикер доступен после привязки заказа к поставке. Повторно уже
            # известные стикеры не скачиваем: barcode заказа WB не меняется.
            pending_ids = [
                int(order_id)
                for order_id, order in orders.items()
                if order.get("supplyId") and order_id not in existing_ids
            ]

            saved = 0
            for i in range(0, len(pending_ids), 100):
                chunk = pending_ids[i:i + 100]
                r = requests.post(
                    WB_API_BASE + "/api/v3/orders/stickers",
                    headers=_wb_headers(),
                    params={"type": "zplv", "width": 40, "height": 30},
                    json={"orders": chunk},
                    timeout=30,
                )
                # Отдельная пачка не должна убить уже сохранённую синхронизацию.
                if r.status_code == 409:
                    time.sleep(0.22)
                    continue
                r.raise_for_status()
                for sticker in (r.json().get("stickers", []) or []):
                    order_id = str(sticker.get("orderId") or "")
                    order = orders.get(order_id)
                    if order and _save_wb_order(order, sticker):
                        saved += 1
                time.sleep(0.22)

            WB_SYNC_STATE.update({
                "last_finished_at": now_iso(),
                "orders_seen": len(orders),
                "stickers_saved": saved,
            })
        except Exception as exc:
            WB_SYNC_STATE["last_error"] = str(exc)
            WB_SYNC_STATE["last_finished_at"] = now_iso()
        finally:
            WB_SYNC_STATE["running"] = False


@app.get("/api/wb/status")
def wb_status():
    init_wms_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    count = query_one("SELECT COUNT(*) FROM wb_orders")
    last_sync = query_one("SELECT MAX(synced_at) FROM wb_orders")
    return jsonify(
        configured=bool(WB_API_TOKEN),
        strict=bool(WB_STRICT_MATCHING),
        local_orders=int(count[0] if count else 0),
        last_sync=(last_sync[0] if last_sync else None),
        sync_days=WB_SYNC_DAYS,
        **dict(WB_SYNC_STATE),
    )


@app.post("/api/wb/sync")
def wb_sync():
    init_wms_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not WB_API_TOKEN:
        return jsonify(error="Сначала добавь WB_API_TOKEN в Render"), 503
    if WB_SYNC_STATE.get("running"):
        return jsonify(ok=True, running=True, message="Синхронизация WB уже идёт"), 202
    threading.Thread(target=sync_wb_orders, name="wb-fbs-sync", daemon=True).start()
    return jsonify(ok=True, running=True), 202


PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111111">
<title>Оприходование палет</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f6f8;color:#111}
.wrap{max-width:560px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:18px;padding:18px;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:14px}
h1{font-size:26px;margin:0 0 8px}
h2{font-size:19px;margin:0 0 10px}
.muted{color:#666;font-size:14px;line-height:1.35}
label{display:block;margin:12px 0 6px;font-size:13px;color:#666}
input,select{width:100%;padding:14px;font-size:16px;border:2px solid #d0d4d9;border-radius:12px;background:#fff}
button{width:100%;padding:15px;font-size:17px;font-weight:700;border:0;border-radius:12px;cursor:pointer;background:#111;color:#fff;margin-top:10px}
button.secondary{background:#e9ebee;color:#111}
button.danger{background:#b42318}
button:disabled{opacity:.45;cursor:not-allowed}
#reader{width:100%;margin-top:14px;overflow:hidden;border-radius:14px}
.hidden{display:none!important}
.ok,.err,.info{margin-top:12px;padding:13px;border-radius:12px}
.ok{background:#eaf8ee}
.err{background:#fdecec}
.info{background:#eef3ff}
.stats{margin-top:14px;padding:14px;background:#f0f2f5;border-radius:12px}
.big{font-size:28px;font-weight:800}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.qtypad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.qtypad button{margin-top:0;padding:13px;font-size:20px;background:#e9ebee;color:#111}
.qtypad button.action{font-size:16px}
.small{font-size:12px;color:#777}
.activeDoc{padding:13px;background:#f0f2f5;border-radius:12px;margin-top:10px}
.nav{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px}
.nav a{display:flex;align-items:center;justify-content:center;text-align:center;min-height:44px;padding:9px 8px;border-radius:12px;text-decoration:none;font-weight:700;font-size:14px;background:#e7edf2;color:#314657;border:1px solid #d2dde5}
.nav a.active{background:#48647a;color:#fff;border-color:#48647a}

@media (orientation:landscape) and (max-height:600px){
 body{overflow:hidden;background:#e9eef3;color:#25313d}
 .wrap{
  max-width:none;width:100%;height:100dvh;margin:0;
  padding:max(5px,env(safe-area-inset-top)) max(7px,env(safe-area-inset-right)) max(5px,env(safe-area-inset-bottom)) max(7px,env(safe-area-inset-left));
  display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);
  grid-template-rows:auto auto auto minmax(0,1fr);gap:5px;overflow:hidden
 }
 .nav{grid-column:1/-1;display:flex;gap:5px;margin:0}
 .nav a{flex:1;min-height:30px;padding:5px 7px;border-radius:8px;font-size:10px}
 .wrap>.small{grid-column:1/-1;margin:0 2px!important;font-size:9px;line-height:1.1}
 .wrap>.card:first-of-type{
  grid-column:1/-1;margin:0;padding:6px 9px;border-radius:10px;
  display:flex;align-items:center;gap:10px;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb
 }
 .wrap>.card:first-of-type h1{font-size:15px;margin:0;white-space:nowrap}
 .wrap>.card:first-of-type .muted{font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #docCard{grid-column:1;grid-row:4;margin:0;min-width:0;height:100%;padding:8px;border-radius:10px;overflow:auto;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb}
 #scanCard{grid-column:2;grid-row:4;margin:0;min-width:0;height:100%;padding:8px;border-radius:10px;overflow:auto;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb}
 #completionCard{position:fixed;inset:8px;z-index:50;margin:0;overflow:auto;border:2px solid #48647a}
 #docCard h2,#scanCard h2{font-size:13px;margin:0 0 4px}
 #docCard .muted,#scanCard .muted{font-size:9px;line-height:1.2}
 label{margin:5px 0 2px;font-size:9px}
 input,select{padding:7px 8px;font-size:11px;border-width:1px;border-radius:8px}
 button{padding:7px 8px;font-size:11px;border-radius:8px;margin-top:5px}
 .activeDoc{padding:7px;margin-top:5px;border-radius:8px;font-size:10px}
 #reader{margin-top:5px;max-height:130px;border-radius:9px}
 .ok,.err,.info{margin-top:5px;padding:7px;border-radius:8px;font-size:10px}
 .stats{margin-top:5px;padding:7px;border-radius:8px}.big{font-size:20px}
 .row{gap:5px}.qtypad{gap:4px;margin-top:5px}.qtypad button{padding:7px;font-size:14px}
 .small{font-size:9px}
}

.disabled-tab{opacity:.45;pointer-events:none}
.step-kicker{font-size:10px;font-weight:900;letter-spacing:.1em;color:#6b7785;margin-bottom:3px}
.scan-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px}
.scan-head h2{font-size:28px;margin:0}
.scan-counters{display:flex;gap:6px}
.scan-counters>div{min-width:72px;background:#eef3f6;border:1px solid #d7e0e7;border-radius:10px;padding:6px 8px;text-align:center}
.scan-counters span{display:block;font-size:22px;font-weight:900;line-height:1}
.scan-counters small{font-size:8px;color:#6b7785}
.quick-row{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.quick-row button{margin-top:5px}
.finish-btn{margin-top:8px}
@media (orientation:landscape) and (max-height:600px){
 .wrap{grid-template-columns:1fr;grid-template-rows:auto auto 1fr}
 .nav{grid-column:1;grid-row:1}
 .wrap>.small{grid-column:1;grid-row:2}
 #docCard,#scanCard{grid-column:1;grid-row:3;width:min(900px,100%);justify-self:center;overflow:auto}
 #scanCard{display:block}
 .scan-head h2{font-size:24px}
 .scan-counters>div{min-width:62px;padding:5px}.scan-counters span{font-size:19px}
 .quick-row button{font-size:10px;padding:6px}
 #reader{max-height:115px}
}
</style>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=1"></head>
<body>
<div class="wrap">
<div class="nav">
  <a class="active" href="/">📦 Приёмка</a>
  <a href="/fbs">🧺 Сборка</a>
  <a href="/move">↔ Перемещение</a>
  <a href="/replenishment">📦 Пополнение <span id="repCountReceiving"></span></a>
</div>
<script>
(function(){
 const n=document.getElementById('repCountReceiving');
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
<div class="small" style="margin:0 2px 10px"><span id="navIdentity"></span> · <a href="/exit">🚪 Завершить смену</a></div>

<div class="card" id="docCard">
  <div class="step-kicker">ПРИЁМКА</div>
  <h2 id="docTitle">Выбери документ</h2>
  <div id="activeDoc" class="activeDoc hidden"></div>
  <div id="docNotice" class="hidden" role="alert"></div>

  <div id="docHome">
    <button id="newDocBtn" onclick="showNewDoc()">＋ Новая приёмка</button>
    <button class="secondary" onclick="showExisting()">Открыть существующую</button>
  </div>

  <div id="newDocForm" class="hidden">
    <label>Склад</label>
    <select id="storeSelect"><option value="">Загрузка…</option></select>
    <label>Поставщик</label>
    <select id="stateSelect"><option value="">Загрузка…</option></select>
    <label>Организация</label>
    <select id="orgSelect"><option value="">Загрузка…</option></select>
    <button onclick="createReceipt()">Начать приёмку</button>
    <button class="secondary" onclick="hideDocForms()">Назад</button>
  </div>

  <div id="existingForm" class="hidden">
    <label>Последние приёмки</label>
    <select id="receiptSelect"><option value="">Загрузка…</option></select>
    <button onclick="useSelectedReceipt()">Открыть</button>
    <button class="secondary" onclick="loadReceipts()">↻ Обновить</button>
    <button class="secondary" onclick="hideDocForms()">Назад</button>
  </div>
</div>

<div class="card hidden" id="scanCard">
  <div class="scan-head">
    <div>
      <div class="step-kicker">ПРИЁМКА</div>
      <h2 id="scanPrompt">Сканируй палету</h2>
      <div id="scanDocName" class="muted"></div>
    </div>
    <div class="scan-counters">
      <div><span id="pallets">0</span><small>палет</small></div>
      <div><span id="units">0</span><small>шт.</small></div>
    </div>
  </div>

  <button id="cameraBtn" onclick="toggleCamera('full')" disabled>📷 Полная палета</button>
  <div class="quick-row">
    <button class="secondary" id="partialBtn" onclick="toggleCamera('partial')" disabled>Неполная</button>
    <button class="secondary" id="oszBtn" onclick="startOszFlow()" disabled>ОСЗ / SSCC</button>
    <button class="secondary" id="manualBtn" onclick="toggleManual()" disabled>ТСД / ввод</button>
  </div>

  <div id="reader" class="hidden"></div>

  <div id="oszForm" class="info hidden">
    <b>ОСЗ / SSCC</b>
    <div class="small" id="oszStatus">Сканируй SSCC палеты.</div>
    <div id="oszProductInfo" class="hidden"></div>
    <label id="oszQtyLabel" class="hidden">Количество, шт.</label>
    <input id="oszQty" class="hidden" inputmode="numeric" type="number" min="1" step="1">
    <div id="oszQtyPad" class="qtypad hidden">
      <button type="button" onclick="oszQtyDigit('1')">1</button><button type="button" onclick="oszQtyDigit('2')">2</button><button type="button" onclick="oszQtyDigit('3')">3</button>
      <button type="button" onclick="oszQtyDigit('4')">4</button><button type="button" onclick="oszQtyDigit('5')">5</button><button type="button" onclick="oszQtyDigit('6')">6</button>
      <button type="button" onclick="oszQtyDigit('7')">7</button><button type="button" onclick="oszQtyDigit('8')">8</button><button type="button" onclick="oszQtyDigit('9')">9</button>
      <button type="button" class="action" onclick="oszQtyClear()">Очистить</button><button type="button" onclick="oszQtyDigit('0')">0</button><button type="button" class="action" onclick="oszQtyBackspace()">⌫</button>
    </div>
    <button id="oszProductBtn" class="secondary hidden" onclick="scanOszProduct()">Сканировать товар</button>
    <button id="oszAcceptBtn" class="hidden" onclick="acceptOszPallet()">Принять палету</button>
    <button class="secondary" onclick="cancelOszFlow()">Отмена</button>
  </div>

  <div id="manual" class="hidden">
    <input id="barcode" inputmode="numeric" autocomplete="off" placeholder="Сканируй штрихкод">
    <button onclick="sendManualFull()">Принять</button>
    <button class="secondary" onclick="sendManualPartial()">Неполная палета</button>
  </div>

  <div id="partialForm" class="info hidden">
    <b id="partialTitle">Неполная палета</b>
    <div class="small" id="partialMeta"></div>
    <label>Фактически, шт.</label>
    <input id="partialQty" inputmode="numeric" type="number" min="1" step="1">
    <button onclick="confirmPartial()">Принять количество</button>
    <button class="secondary" onclick="cancelPartial()">Отмена</button>
  </div>

  <div id="result"></div>
  <button id="completeBtn" class="danger finish-btn" onclick="completeReceipt()" disabled>✓ Завершить приёмку</button>
</div>

<div class="card hidden" id="completionCard">
  <div class="step-kicker">ГОТОВО</div>
  <h1>✅ Приёмка завершена</h1>
  <div id="completionSummary" class="stats"></div>
  <button onclick="startAnotherReceipt()">＋ Новая приёмка</button>
</div></div>

</div>

<script>
let scanner=null;
let pallets=0;
let units=0;
let busy=false;
let activeReceiptId='';
let activeReceiptName='';
let audioCtx=null;
let scanMode='full';
let partialBarcode='';
let partialStandardQty=0;
let partialMeta=null;
let stagedPallet=null;
let pendingConfirmation=null;
const RECEIVING_PENDING_KEY='onix_receiving_confirmation_v1';
let lastBarcode='';
let lastScanAt=0;
let oszSscc='';
let oszProductBarcode='';
let oszProductMeta=null;

function initAudio(){
  try{
    if(!audioCtx){
      const Ctx=window.AudioContext||window.webkitAudioContext;
      if(Ctx)audioCtx=new Ctx();
    }
    if(audioCtx && audioCtx.state==='suspended')audioCtx.resume();
  }catch(e){}
}

function playTone(freq=880,duration=0.12,type='sine'){
  try{
    initAudio();
    if(!audioCtx)return;
    const osc=audioCtx.createOscillator();
    const gain=audioCtx.createGain();
    osc.type=type;
    osc.frequency.value=freq;
    gain.gain.setValueAtTime(0.0001,audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18,audioCtx.currentTime+0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001,audioCtx.currentTime+duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime+duration+0.02);
  }catch(e){}
}

function successFeedback(){
  playTone(980,0.10,'sine');
  setTimeout(()=>playTone(1320,0.12,'sine'),110);
  try{if(navigator.vibrate)navigator.vibrate([80,40,80]);}catch(e){}
}

function errorFeedback(){
  playTone(220,0.22,'square');
  try{if(navigator.vibrate)navigator.vibrate([180,80,180]);}catch(e){}
}

const stateSelect=document.getElementById('stateSelect');
const orgSelect=document.getElementById('orgSelect');
const storeSelect=document.getElementById('storeSelect');
const receiptSelect=document.getElementById('receiptSelect');

async function activateReceivingModule(){
  const shiftId=localStorage.getItem('fbs_shift_id')||'';
  if(!shiftId)return;
  try{
    await fetch('/api/fbs/activity',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({shift_id:shiftId,activity_type:'receiving'})
    });
  }catch(e){}
}
activateReceivingModule();

function setActiveReceipt(id,name,meta=''){
  activeReceiptId=id||'';
  if(activeReceiptId)document.getElementById('docNotice').classList.add('hidden');
  activeReceiptName=name||'';
  localStorage.setItem('receipt_id',activeReceiptId);
  localStorage.setItem('receipt_name',activeReceiptName);

  const box=document.getElementById('activeDoc');
  const enabled=!!activeReceiptId;
  document.getElementById('cameraBtn').disabled=!enabled;
  document.getElementById('partialBtn').disabled=!enabled;
  document.getElementById('oszBtn').disabled=!enabled;
  document.getElementById('manualBtn').disabled=!enabled;
  document.getElementById('completeBtn').disabled=!enabled;

  if(enabled){
    box.classList.remove('hidden');
    box.innerHTML='<b>'+escapeHtml(activeReceiptName)+'</b><br><span class="small">'+escapeHtml(meta||'Активная приёмка')+'</span>';
    document.getElementById('scanDocName').textContent=activeReceiptName;
    document.getElementById('docCard').classList.add('hidden');
    document.getElementById('scanCard').classList.remove('hidden');
  }else{
    box.classList.add('hidden');
    box.innerHTML='';
    document.getElementById('scanDocName').textContent='';
    document.getElementById('scanCard').classList.add('hidden');
    document.getElementById('docCard').classList.remove('hidden');
  }
}

const RECEIPT_UNAVAILABLE_REASONS=['deleted','missing','completed'];
let receiptToRecheck=null;

function showUnavailableReceipt(info,name=''){
  // Never change or restore MoySklad documents. Only forget the invalid selection.
  const n=String(info.document_name||name||'').trim();
  if(scanner)stopScanner().catch(()=>{});
  stagedPallet=null;
  cancelPartial();
  resetOszForm();
  document.getElementById('oszForm').classList.add('hidden');
  setActiveReceipt('','','');
  hideDocForms();
  document.getElementById('docCard').classList.remove('hidden');
  const box=document.getElementById('docNotice');
  const description=info.reason==='completed'
    ? 'уже проведено в МойСклад'
    : info.reason==='missing'
      ? 'больше недоступно в МойСклад'
      : 'находится в корзине МойСклад';
  box.innerHTML='<div class="err"><b>Оприходование недоступно</b><br>'+
    (n?'Документ №'+escapeHtml(n)+' ':'Документ ')+escapeHtml(description)+'.<br>'+
    'Выберите другое оприходование или начните новую приёмку.</div>'+
    '<div class="quick-row">'+
    '<button type="button" class="secondary" onclick="showExisting()">Выбрать другое</button>'+
    '<button type="button" onclick="showNewDoc()">＋ Новая приёмка</button></div>';
  box.classList.remove('hidden');
  errorFeedback();
}

async function openReceiptSafely(id,name,meta=''){
  if(!id)return false;
  if(stagedPallet||pendingConfirmation){
    showError('Сначала заверши или сверь предыдущую операцию приёмки.');
    return false;
  }
  // Refuse scans until a fresh MoySklad check succeeds.
  receiptToRecheck={id,name,meta};
  setActiveReceipt('','','');
  document.getElementById('docCard').classList.remove('hidden');
  const notice=document.getElementById('docNotice');
  notice.innerHTML='<div class="info">Проверяю документ в МойСклад…</div>';
  notice.classList.remove('hidden');
  try{
    const response=await fetch('/api/receipts/'+encodeURIComponent(id)+'/status',{cache:'no-store'});
    const data=await response.json();
    if(!response.ok){
      if(RECEIPT_UNAVAILABLE_REASONS.includes(data.reason)){
        showUnavailableReceipt(data,name);
      }else{
        notice.innerHTML='<div class="err">Не удалось проверить документ в МойСклад. Повтори проверку после восстановления связи.</div>'+
          '<button type="button" onclick="retryReceiptCheck()">Повторить проверку</button>';
      }
      return false;
    }
    setActiveReceipt(id,data.name||name,meta);
    return true;
  }catch(e){
    notice.innerHTML='<div class="err">Нет связи с МойСклад. Документ пока недоступен для сканирования.</div>'+
      '<button type="button" onclick="retryReceiptCheck()">Повторить проверку</button>';
    return false;
  }
}

function retryReceiptCheck(){
  if(receiptToRecheck){const r=receiptToRecheck;openReceiptSafely(r.id,r.name,r.meta);}
}

function showNewDoc(){
  document.getElementById('docNotice').classList.add('hidden');
  document.getElementById('docHome').classList.add('hidden');
  document.getElementById('newDocForm').classList.remove('hidden');
  document.getElementById('existingForm').classList.add('hidden');
  document.getElementById('docTitle').textContent='Новая приёмка';
}
function showExisting(){
  document.getElementById('docNotice').classList.add('hidden');
  document.getElementById('docHome').classList.add('hidden');
  document.getElementById('existingForm').classList.remove('hidden');
  document.getElementById('newDocForm').classList.add('hidden');
  document.getElementById('docTitle').textContent='Открыть приёмку';
  loadReceipts();
}
function hideDocForms(){
  document.getElementById('newDocForm').classList.add('hidden');
  document.getElementById('existingForm').classList.add('hidden');
  document.getElementById('docHome').classList.remove('hidden');
  document.getElementById('docTitle').textContent='Выбери документ';
}

async function loadBootstrap(){
  try{
    const r=await fetch('/api/bootstrap');
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось загрузить справочники');

    fillSelect(storeSelect,data.stores,'Выбери склад');
    fillSelect(stateSelect,data.states,'Выбери поставщика / статус');
    fillSelect(orgSelect,data.organizations,'Выбери организацию');

    if(data.organizations.length===1)orgSelect.value=data.organizations[0].id;
    if(data.stores.length===1)storeSelect.value=data.stores[0].id;
  }catch(e){
    showError(e.message);
  }
}

function fillSelect(el,items,placeholder){
  el.innerHTML='<option value="">'+placeholder+'</option>';
  items.forEach(x=>{
    const o=document.createElement('option');
    o.value=x.id;
    o.textContent=x.name;
    el.appendChild(o);
  });
}

async function createReceipt(){
  if(stagedPallet||pendingConfirmation){showError('Сначала заверши предыдущую операцию приёмки.');return;}
  const stateId=stateSelect.value;
  const organizationId=orgSelect.value;
  const storeId=storeSelect.value;

  if(!stateId||!organizationId||!storeId){
    showError('Выбери склад, поставщика/статус и организацию.');
    return;
  }

  try{
    const r=await fetch('/api/receipts',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        state_id:stateId,
        organization_id:organizationId,
        store_id:storeId
      })
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось создать Оприходование');

    pallets=0; units=0;
    document.getElementById('pallets').textContent='0';
    document.getElementById('units').textContent='0';

    setActiveReceipt(data.id,data.name,data.state+' · '+data.store+' · черновик');
    hideDocForms();
    document.getElementById('result').innerHTML='<div class="ok">Оприходование создано. Можно сканировать палеты.</div>';
  }catch(e){
    showError(e.message);
  }
}

async function loadReceipts(){
  receiptSelect.innerHTML='<option value="">Загружаю…</option>';
  try{
    const r=await fetch('/api/receipts');
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось загрузить Приёмки');

    receiptSelect.innerHTML='<option value="">Выбери Приёмку</option>';
    data.receipts.forEach(x=>{
      const o=document.createElement('option');
      o.value=x.id;
      o.textContent=x.label;
      o.dataset.name=x.name;
      o.dataset.meta=x.meta;
      receiptSelect.appendChild(o);
    });
  }catch(e){
    receiptSelect.innerHTML='<option value="">Ошибка загрузки</option>';
    showError(e.message);
  }
}

async function useSelectedReceipt(){
  if(stagedPallet||pendingConfirmation){showError('Сначала заверши предыдущую операцию приёмки.');return;}
  const o=receiptSelect.options[receiptSelect.selectedIndex];
  if(!o||!o.value){showError('Выбери Приёмку.');return;}
  const ok=await openReceiptSafely(o.value,o.dataset.name||o.textContent,o.dataset.meta||'');
  if(ok)hideDocForms();
}

async function completeReceipt(){
  if(!activeReceiptId)return;
  if(stagedPallet||pendingConfirmation){showError('Сначала заверши или сверь предыдущую палету.');return;}
  if(!confirm('Провести это Оприходование? После этого документ будет проведён в МойСклад.'))return;

  try{
    if(scanner)await stopScanner();

    const fallbackPallets=pallets;
    const fallbackUnits=units;
    const r=await fetch('/api/receipts/'+activeReceiptId+'/complete',{method:'POST'});
    const data=await r.json();
    if(!r.ok){
      if(RECEIPT_UNAVAILABLE_REASONS.includes(data.reason)){
        showUnavailableReceipt(data,activeReceiptName);
        return;
      }
      throw new Error(data.error||'Не удалось провести Оприходование');
    }

    setActiveReceipt('','','');

    document.getElementById('docCard').classList.add('hidden');
    document.getElementById('scanCard').classList.add('hidden');
    document.getElementById('completionCard').classList.remove('hidden');

    const acceptedPallets=Number(data.pallets||fallbackPallets||0);
    const acceptedUnits=Number(data.total_units||fallbackUnits||0);
    const positions=Number(data.positions||0);
    const employee=data.employee||document.getElementById('navIdentity').textContent||'';

    document.getElementById('completionSummary').innerHTML=
      '<div style="margin-bottom:10px"><b>'+escapeHtml(data.name||'Оприходование')+'</b></div>'+
      '<div class="row">'+
        '<div><div class="small">Палет принято</div><div class="big">'+acceptedPallets+'</div></div>'+
        '<div><div class="small">Единиц товара</div><div class="big">'+acceptedUnits+'</div></div>'+
      '</div>'+
      '<div style="margin-top:12px"><b>Позиций:</b> '+positions+'</div>'+
      (employee?'<div style="margin-top:6px"><b>Сотрудник:</b> '+escapeHtml(employee)+'</div>':'')+
      '<div style="margin-top:6px"><b>Статус:</b> Проведено</div>';
  }catch(e){
    showError(e.message);
  }
}

function startAnotherReceipt(){
  pallets=0;
  units=0;
  document.getElementById('pallets').textContent='0';
  document.getElementById('units').textContent='0';
  document.getElementById('result').innerHTML='';
  document.getElementById('completionCard').classList.add('hidden');
  document.getElementById('docCard').classList.remove('hidden');
  document.getElementById('scanCard').classList.remove('hidden');
  showNewDoc();
}

function toggleManual(){
  initAudio();
  document.getElementById('manual').classList.toggle('hidden');
  setTimeout(()=>document.getElementById('barcode').focus(),100);
}

async function stopScanner(){
  if(scanner){
    try{await scanner.stop();}catch(e){}
    scanner=null;
  }
  document.getElementById('reader').classList.add('hidden');
  document.getElementById('cameraBtn').textContent='📷 Сканировать полную палету';
  document.getElementById('partialBtn').textContent='📦 Неполная палета';
  document.getElementById('oszBtn').textContent='🏭 Палета ОСЗ / SSCC';
}

async function toggleCamera(mode='full'){
  initAudio();
  if(!activeReceiptId){showError('Сначала создай или открой Оприходование.');return;}
  if(stagedPallet||pendingConfirmation){showError('Сначала заверши подтверждение предыдущей палеты.');return;}

  if(scanner){
    await stopScanner();
    return;
  }

  scanMode=mode;

  if(typeof Html5Qrcode==='undefined'){
    showError('Модуль сканирования не загрузился. Обнови страницу.');
    return;
  }

  const scanWidth=Math.max(240,Math.min(360,window.innerWidth-56));
  const formats=(mode==='osz_sscc')
    ? [Html5QrcodeSupportedFormats.CODE_128]
    : [Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.CODE_128];

  document.getElementById('reader').classList.remove('hidden');
  document.getElementById('result').innerHTML='<div class="info">Наведи камеру на ШК целиком и держи его горизонтально.</div>';
  scanner=new Html5Qrcode("reader");

  try{
    await scanner.start(
      {facingMode:"environment"},
      {
        fps:20,
        qrbox:{width:scanWidth,height:110},
        disableFlip:true,
        formatsToSupport:formats
      },
      async(decodedText)=>{
        if(busy)return;
        const now=Date.now();
        if(decodedText===lastBarcode && now-lastScanAt<1800)return;
        lastBarcode=decodedText;
        lastScanAt=now;

        // Камеру закрываем сразу после распознавания, не ждём сеть/API.
        // Так пользователь мгновенно понимает, что код уже считан,
        // и камера не успевает повторно поймать тот же штрихкод.
        await stopScanner();
        document.getElementById('result').innerHTML='<div class="info"><b>ШК считан.</b> Обрабатываю…</div>';

        if(scanMode==='partial') await preparePartial(decodedText);
        else if(scanMode==='osz_sscc') await handleOszSscc(decodedText);
        else if(scanMode==='osz_product') await prepareOszProduct(decodedText);
        else await sendScan(decodedText);
      },
      ()=>{}
    );
    if(scanMode==='partial') document.getElementById('partialBtn').textContent='⏹ Остановить камеру';
    else if(scanMode==='osz_sscc'||scanMode==='osz_product') document.getElementById('oszBtn').textContent='⏹ Остановить камеру';
    else document.getElementById('cameraBtn').textContent='⏹ Остановить камеру';
  }catch(e){
    await stopScanner();
    const message=String((e&&e.message)||e||'');
    if(message.includes('expected pattern')){
      showError('Safari не принял ускоренный режим камеры. Переключил на совместимый режим, попробуйте скан ещё раз.');
    }else{
      showError('Не удалось открыть камеру: '+message);
    }
  }
}

function looksLikeSscc(code){
  const raw=String(code||'').trim().replace(/\s+/g,'');
  if(raw.startsWith(']C1'))return true;
  if(/^\(00\)\d{18}$/.test(raw))return true;
  if(/^00\d{18}$/.test(raw))return true;
  if(/^\d{18}$/.test(raw))return true;
  return false;
}

async function sendManualFull(){
  if(stagedPallet||pendingConfirmation){showError('Сначала подтверди предыдущую палету.');return;}
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;

  // Очищаем поле сразу, потому что HID-сканер уже передал весь код.
  // Дальше результат операции показывается отдельным статусом.
  input.value='';

  // Если уже считан SSCC ОСЗ, следующий HID-скан считаем ШК товара.
  if(oszSscc && !oszProductBarcode){
    document.getElementById('result').innerHTML='<div class="info"><b>ШК товара считан.</b> Ищу товар…</div>';
    await prepareOszProduct(code);
    return;
  }

  // HID-сканер может отдавать GS1 SSCC как (00)+18 цифр, 00+18 цифр
  // или только 18 цифр. Валидность окончательно проверяет сервер.
  if(looksLikeSscc(code)){
    document.getElementById('oszForm').classList.remove('hidden');
    document.getElementById('result').innerHTML='<div class="info"><b>SSCC считан.</b> Проверяю палету…</div>';
    await handleOszSscc(code,false);
    if(oszSscc){
      setTimeout(()=>input.focus(),80);
    }
    return;
  }

  await sendScan(code);
  input.focus();
}

async function sendManualPartial(){
  if(stagedPallet||pendingConfirmation){showError('Сначала подтверди предыдущую палету.');return;}
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;
  await preparePartial(code);
  input.value='';
}

async function preparePartial(barcode){
  if(stagedPallet||pendingConfirmation){showError('Сначала подтверди предыдущую палету.');return;}
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  busy=true;
  try{
    const r=await fetch('/api/pack-info',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось определить упаковку');

    await stopScanner();

    partialBarcode=barcode;
    partialStandardQty=Number(data.standard_qty||0);
    partialMeta=data;
    document.getElementById('partialTitle').textContent=(data.article||data.name||'Неполная палета');
    document.getElementById('partialMeta').textContent='Стандартная палета: '+partialStandardQty+' шт. · '+barcode;
    document.getElementById('partialQty').value='';
    document.getElementById('partialQty').max=String(partialStandardQty);
    document.getElementById('partialForm').classList.remove('hidden');
    setTimeout(()=>document.getElementById('partialQty').focus(),100);
    successFeedback();
  }catch(e){
    showError(e.message);
  }finally{
    busy=false;
  }
}

function cancelPartial(){
  partialBarcode='';
  partialStandardQty=0;
  partialMeta=null;
  document.getElementById('partialForm').classList.add('hidden');
}

async function confirmPartial(){
  const qty=Number(document.getElementById('partialQty').value);
  if(!partialBarcode){showError('Сначала отсканируй палету.');return;}
  if(!Number.isFinite(qty)||qty<=0){showError('Укажи фактическое количество больше нуля.');return;}
  if(qty>partialStandardQty){showError('Фактическое количество не может быть больше стандартной палеты '+partialStandardQty+' шт.');return;}

  const barcode=partialBarcode;
  const data=partialMeta;
  cancelPartial();
  await submitReceivingPallet(makeReceivingPallet(barcode,qty,data));
}

function resetOszForm(){
  oszSscc='';
  oszProductBarcode='';
  oszProductMeta=null;
  document.getElementById('oszStatus').textContent='Сначала отсканируйте SSCC палеты.';
  document.getElementById('oszProductInfo').classList.add('hidden');
  document.getElementById('oszProductInfo').innerHTML='';
  document.getElementById('oszQtyLabel').classList.add('hidden');
  document.getElementById('oszQty').classList.add('hidden');
  document.getElementById('oszQtyPad').classList.add('hidden');
  document.getElementById('oszQty').value='';
  document.getElementById('oszProductBtn').classList.add('hidden');
  document.getElementById('oszAcceptBtn').classList.add('hidden');
}

async function startOszFlow(){
  if(stagedPallet||pendingConfirmation){showError('Сначала подтверди предыдущую палету.');return;}
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  resetOszForm();
  document.getElementById('oszForm').classList.remove('hidden');
  await toggleCamera('osz_sscc');
}

async function handleOszSscc(raw,autoCamera=true){
  busy=true;
  try{
    const r=await fetch('/api/osz/check',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sscc:raw})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось проверить SSCC');

    await stopScanner();
    oszSscc=data.sscc;
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>'+
      (autoCamera?'SSCC принят. Готовлю сканирование ШК товара…':'SSCC принят. Теперь отсканируйте ШК товара этим же сканером.');
    document.getElementById('oszProductBtn').classList.remove('hidden');
    successFeedback();

    // Для камеры следующий этап открываем автоматически.
    // Для Bluetooth/HID оставляем фокус в поле ТСД: следующий пик будет ШК товара.
    if(autoCamera){
      setTimeout(()=>{
        if(oszSscc && !scanner && !oszProductBarcode){
          toggleCamera('osz_product');
        }
      },250);
    }
  }catch(e){
    await stopScanner();
    showError(e.message);
  }finally{
    busy=false;
  }
}

async function scanOszProduct(){
  if(!oszSscc){showError('Сначала отсканируйте SSCC палеты ОСЗ.');return;}
  await toggleCamera('osz_product');
}

async function prepareOszProduct(barcode){
  busy=true;
  try{
    const r=await fetch('/api/osz/product-info',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Товар не найден');

    await stopScanner();
    oszProductBarcode=barcode;
    oszProductMeta=data;
    document.getElementById('oszProductInfo').innerHTML=
      '<div style="margin-top:10px"><b>'+escapeHtml(data.article||data.name||'Товар')+'</b><br>'+
      '<span class="small">'+escapeHtml(data.name||'')+
      (data.standard_qty?'<br>Квант МойСклад: '+data.standard_qty+' шт.':'')+
      '</span></div>';
    document.getElementById('oszProductInfo').classList.remove('hidden');
    document.getElementById('oszQtyLabel').classList.remove('hidden');
    document.getElementById('oszQty').classList.remove('hidden');
    document.getElementById('oszQtyPad').classList.remove('hidden');
    document.getElementById('oszQty').value='';
    document.getElementById('oszAcceptBtn').classList.remove('hidden');
    document.getElementById('oszProductBtn').classList.add('hidden');
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>Проверьте количество по этикетке ОСЗ.';
    successFeedback();
  }catch(e){
    await stopScanner();
    showError(e.message);
  }finally{
    busy=false;
  }
}

function oszQtyDigit(digit){
  const input=document.getElementById('oszQty');
  const current=String(input.value||'');
  if(current.length>=7)return;
  input.value=(current==='0'?'':current)+digit;
}

function oszQtyBackspace(){
  const input=document.getElementById('oszQty');
  input.value=String(input.value||'').slice(0,-1);
}

function oszQtyClear(){
  document.getElementById('oszQty').value='';
}

async function acceptOszPallet(){
  if(busy)return;
  if(!oszSscc||!oszProductBarcode){showError('Сначала отсканируйте SSCC и товар.');return;}
  const qty=Number(document.getElementById('oszQty').value);
  if(!Number.isFinite(qty)||qty<=0){showError('Введите количество с этикетки ОСЗ.');return;}

  busy=true;
  try{
    const r=await fetch('/api/osz/accept',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        receipt_id:activeReceiptId,
        sscc:oszSscc,
        product_barcode:oszProductBarcode,
        quantity:qty
      })
    });
    const data=await r.json();
    if(!r.ok){
      if(RECEIPT_UNAVAILABLE_REASONS.includes(data.reason)){
        showUnavailableReceipt(data,activeReceiptName);
        return;
      }
      throw new Error(data.error||'Не удалось принять палету ОСЗ');
    }

    pallets+=1;
    units+=Number(data.added||0);
    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;
    successFeedback();
    document.getElementById('result').innerHTML=
      '<div class="ok"><b>Палета ОСЗ принята</b><br>'+
      escapeHtml(data.article||data.name||'')+' · <b>'+data.added+' шт.</b><br>'+
      '<span class="small">SSCC '+escapeHtml(data.sscc)+'</span></div>';

    resetOszForm();
    document.getElementById('oszForm').classList.add('hidden');

    // При работе Bluetooth/HID сканером сразу готовы к следующей палете.
    const manual=document.getElementById('manual');
    if(manual && !manual.classList.contains('hidden')){
      setTimeout(()=>document.getElementById('barcode').focus(),80);
    }
  }catch(e){
    showError(e.message);
  }finally{
    busy=false;
  }
}

async function cancelOszFlow(){
  if(scanner)await stopScanner();
  resetOszForm();
  document.getElementById('oszForm').classList.add('hidden');
}

function makeReceivingPallet(barcode,quantity,meta){
  if(!window.crypto||!crypto.randomUUID){
    showError('Устройство не поддерживает безопасный номер операции. Обнови браузер.');
    return null;
  }
  return {
    operation_id:crypto.randomUUID(),
    receipt_id:activeReceiptId,
    barcode:String(barcode),
    quantity:quantity,
    article:(meta&&meta.article)||'',
    name:(meta&&meta.name)||'',
    pack_name:(meta&&meta.pack_name)||'',
    standard_qty:(meta&&meta.standard_qty)||0
  };
}

function showStagedPallet(){
  if(!stagedPallet)return;
  const p=stagedPallet;
  document.getElementById('result').innerHTML=
    '<div class="info"><b>Палета готова к приёмке</b><br>'+
    escapeHtml(p.article||p.name||'Товар')+' · <b>'+escapeHtml(p.quantity===null?p.standard_qty:p.quantity)+' шт.</b><br>'+
    '<span class="small">'+escapeHtml(p.barcode)+' · Пока ничего не добавлено</span><br>'+
    '<button type="button" onclick="confirmStagedPallet()">✓ Подтвердить 1 палету</button> '+
    '<button type="button" class="secondary" onclick="cancelStagedPallet()">Отмена</button></div>';
}
function cancelStagedPallet(){
  if(pendingConfirmation)return;
  stagedPallet=null;
  document.getElementById('result').innerHTML='<div class="info">Скан отменён, количество не менялось.</div>';
}
async function confirmStagedPallet(){
  if(busy||!stagedPallet)return;
  await submitReceivingPallet(stagedPallet);
}
function clearConfirmedPallet(response,pending){
  pendingConfirmation=null;
  localStorage.removeItem(RECEIVING_PENDING_KEY);
  // One count per confirmed operation in this local session.
  pallets+=1;
  units+=Number(response.added||0);
  document.getElementById('pallets').textContent=pallets;
  document.getElementById('units').textContent=units;
  successFeedback();
  document.getElementById('result').innerHTML=
    '<div class="ok"><b>Палета принята</b><br>'+
    escapeHtml(response.article||response.name||pending.article||'')+' · <b>'+escapeHtml(response.added)+' шт.</b><br>'+
    'В МойСклад: <b>'+escapeHtml(response.total)+' шт.</b><br>'+
    '<span class="small">'+escapeHtml(pending.barcode)+'</span></div>';
}
function showUncertainPallet(){
  const p=pendingConfirmation;
  if(!p)return;
  document.getElementById('result').innerHTML=
    '<div class="err"><b>Результат приёмки требует проверки</b><br>'+
    escapeHtml(p.article||p.name||p.barcode)+' · '+escapeHtml(p.quantity===null?p.standard_qty:p.quantity)+' шт.<br>'+
    'Не сканируй палету снова: запись могла пройти в МойСклад.<br>'+
    '<button type="button" onclick="checkReceivingPallet()">Проверить состояние</button> '+
    '<button type="button" class="secondary" onclick="reconcileReceivingPallet()">Сверил в МойСклад</button></div>';
}
async function checkReceivingPallet(){
  if(busy||!pendingConfirmation)return;
  busy=true;
  try{
    const p=pendingConfirmation;
    const r=await fetch('/api/scan-operation/'+encodeURIComponent(p.operation_id),{cache:'no-store'});
    const d=await r.json();
    if(r.ok&&d.status==='done'){
      clearConfirmedPallet(d.response,p);
    }else if(d.status==='rejected' && RECEIPT_UNAVAILABLE_REASONS.includes(d.reason)){
      pendingConfirmation=null;
      localStorage.removeItem(RECEIVING_PENDING_KEY);
      showUnavailableReceipt(d,activeReceiptName);
    }else if(r.status===404){
      // Same UUID, never a second distinct operation.
      busy=false;
      await submitReceivingPallet(p);
      return;
    }else{
      showUncertainPallet();
    }
  }catch(e){showUncertainPallet();}
  finally{busy=false;}
}
async function reconcileReceivingPallet(){
  if(busy||!pendingConfirmation)return;
  if(!confirm('Сначала сверь количество в документе МойСклад. Операция не будет автоматически повторена. Ты проверил результат?'))return;
  busy=true;
  try{
    const p=pendingConfirmation;
    const r=await fetch('/api/scan-operation/'+encodeURIComponent(p.operation_id)+'/reconcile',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({verified:true})
    });
    const d=await r.json();
    if(!r.ok)throw new Error(d.error||'Не удалось закрыть проверку');
    if(d.status==='done'){
      clearConfirmedPallet(d.response,p);
      return;
    }
    pendingConfirmation=null;
    localStorage.removeItem(RECEIVING_PENDING_KEY);
    document.getElementById('result').innerHTML='<div class="info">Сверка отмечена. ONIX не прибавляла товар повторно.</div>';
  }catch(e){showError(e.message);}
  finally{busy=false;}
}
async function submitReceivingPallet(p){
  if(!p||busy)return;
  if(pendingConfirmation&&pendingConfirmation.operation_id!==p.operation_id){
    showUncertainPallet();return;
  }
  busy=true;
  stagedPallet=null;
  pendingConfirmation=p;
  // Save BEFORE network request. Browser restart cannot silently issue a new UUID.
  localStorage.setItem(RECEIVING_PENDING_KEY,JSON.stringify(p));
  document.getElementById('result').innerHTML='<div class="info"><b>Подтверждаю палету в МойСклад…</b></div>';
  try{
    const r=await fetch('/api/scan',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(p)
    });
    const d=await r.json();
    if(!r.ok){
      if(RECEIPT_UNAVAILABLE_REASONS.includes(d.reason)){
        // Only discard the local UUID when the backend has definitively
        // rejected the operation without changing the document.
        pendingConfirmation=null;
        localStorage.removeItem(RECEIVING_PENDING_KEY);
        showUnavailableReceipt(d,activeReceiptName);
        return;
      }
      throw new Error(d.error||'Результат не подтверждён');
    }
    clearConfirmedPallet(d,p);
  }catch(e){
    showUncertainPallet();
  }finally{busy=false;}
}
async function sendScan(barcode,quantity=null){
  if(busy)return;
  if(stagedPallet||pendingConfirmation){showError('Подтверди предыдущую палету.');return;}
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  busy=true;
  try{
    const r=await fetch('/api/pack-info',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Упаковка не найдена');
    stagedPallet=makeReceivingPallet(barcode,quantity,data);
    if(stagedPallet){
      await stopScanner();
      showStagedPallet();
      successFeedback();
    }
  }catch(e){showError(e.message);}
  finally{busy=false;}
}

function showError(message){
  errorFeedback();
  document.getElementById('result').innerHTML='<div class="err">'+escapeHtml(String(message))+'</div>';
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
}

document.getElementById('barcode').addEventListener('keydown',e=>{
  if(e.key==='Enter')sendManualFull();
});

async function loadWmsIdentity(){
  try{
    const r=await fetch('/api/auth/me',{cache:'no-store'});
    if(!r.ok){location.href='/login';return;}
    const d=await r.json();
    const employeeId=d.employee.id;
    const previousEmployee=localStorage.getItem('onix_employee_id');
    // The one shared phone must not show a previous employee's receipt
    // after an authorized QR handover. Normal browser close preserves it.
    if(previousEmployee && previousEmployee!==employeeId){
      localStorage.removeItem('receipt_id');
      localStorage.removeItem('receipt_name');
    }
    localStorage.setItem('onix_employee_id',employeeId);
    document.getElementById('navIdentity').textContent=d.employee.name+(d.employee.badge_number?' · бейдж №'+d.employee.badge_number:'');
    const op=await fetch('/api/operation/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'receiving'})});
    if(!op.ok)throw Error('Не удалось восстановить работу с приёмкой');
    const savedId=localStorage.getItem('receipt_id')||'';
    const savedName=localStorage.getItem('receipt_name')||'';
    try{
      const savedPending=JSON.parse(localStorage.getItem(RECEIVING_PENDING_KEY)||'null');
      if(savedPending&&savedPending.operation_id&&savedPending.receipt_id){
        pendingConfirmation=savedPending;
      }
    }catch(e){localStorage.removeItem(RECEIVING_PENDING_KEY);}
    const selectedId=(pendingConfirmation&&pendingConfirmation.receipt_id)||savedId;
    if(selectedId&&pendingConfirmation)setActiveReceipt(selectedId,savedName||'Ранее выбранная Приёмка','Сохранено на этом телефоне');
    if(pendingConfirmation){
      // A previously submitted operation must be reconciled, never silently
      // discarded on page reload, even if the document was later removed.
      showUncertainPallet();
    }else if(savedId){
      await openReceiptSafely(savedId,savedName||'Ранее выбранная Приёмка','Сохранено на этом телефоне');
    }
  }catch(e){
    showError('Не удалось восстановить рабочую приёмку. Проверь подключение к сети и обнови страницу.');
  }
}
loadWmsIdentity();
loadBootstrap();
setTimeout(()=>fetch('/api/warmup',{method:'POST'}).catch(()=>{}),2500);
</script>
</body>
</html>"""

def headers():
    if not TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN не задан")
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }

def meta(entity_type, entity_id):
    return {
        "meta": {
            "href": f"{API_BASE}/entity/{entity_type}/{entity_id}",
            "type": entity_type,
            "mediaType": "application/json",
        }
    }

def get_rows(entity, params=None):
    r = requests.get(
        f"{API_BASE}/entity/{entity}",
        headers=headers(),
        params=params or {"limit": 1000},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("rows", [])

def get_enter_states():
    r = requests.get(
        f"{API_BASE}/entity/enter/metadata",
        headers=headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("states", []) or []

def bootstrap_data():
    now_ts = time.time()
    with _BOOTSTRAP_CACHE_LOCK:
        cached = _BOOTSTRAP_CACHE.get("data")
        expires_at = float(_BOOTSTRAP_CACHE.get("expires_at", 0) or 0)
        if cached is not None and expires_at > now_ts:
            return cached

    with ThreadPoolExecutor(max_workers=3) as pool:
        org_future = pool.submit(get_rows, "organization", {"limit": 1000, "order": "name"})
        store_future = pool.submit(get_rows, "store", {"limit": 1000, "order": "name"})
        state_future = pool.submit(get_enter_states)
        organizations = org_future.result()
        stores = store_future.result()
        states = state_future.result()

    data = {
        "organizations": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in organizations if not x.get("archived")],
        "stores": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in stores if not x.get("archived")],
        "states": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in states],
    }
    with _BOOTSTRAP_CACHE_LOCK:
        _BOOTSTRAP_CACHE["data"] = data
        _BOOTSTRAP_CACHE["expires_at"] = time.time() + BOOTSTRAP_CACHE_SECONDS
    return data

def list_receipts():
    rows = get_rows("enter", {
        "limit": 30,
        "order": "moment,desc",
        "expand": "state,store",
    })
    result = []
    for row in rows:
        moment = (row.get("moment") or "")[:16].replace("T", " ")
        name = row.get("name") or "Без номера"
        state = (row.get("state") or {}).get("name") or "без статуса"
        store = (row.get("store") or {}).get("name") or "без склада"
        status = "проведено" if row.get("applicable") else "черновик"
        result.append({
            "id": row.get("id"),
            "name": name,
            "label": f"{name} · {moment} · {store} · {state} · {status}",
            "meta": f"{store} · {state} · {status}",
        })
    return result

def create_receipt(state_id, organization_id, store_id):
    states = get_enter_states()
    state = next((x for x in states if x.get("id") == state_id), None)
    if not state or not state.get("meta"):
        raise ValueError("Выбранный статус Оприходования не найден.")

    payload = {
        "organization": meta("organization", organization_id),
        "store": meta("store", store_id),
        "state": {"meta": state["meta"]},
        "applicable": False,
        "description": "Оприходование создано мобильным сканером палет",
    }
    r = requests.post(
        f"{API_BASE}/entity/enter",
        headers=headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def complete_receipt(receipt_id):
    r = requests.put(
        f"{API_BASE}/entity/enter/{receipt_id}",
        headers=headers(),
        json={"applicable": True},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def _barcode_values(value):
    values = []
    if value is None:
        return values
    if isinstance(value, str):
        s = value.strip()
        if s:
            values.append(s)
        return values
    if isinstance(value, (int, float)):
        values.append(str(value).strip())
        return values
    if isinstance(value, list):
        for item in value:
            values.extend(_barcode_values(item))
        return values
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if "barcode" in key_l or key_l in ("ean13", "ean8", "code128", "qr"):
                values.extend(_barcode_values(item))
            elif isinstance(item, (dict, list)):
                values.extend(_barcode_values(item))
        return values
    return values

def _pack_barcodes(pack):
    values = []
    values.extend(_barcode_values(pack.get("barcode")))
    values.extend(_barcode_values(pack.get("barcodes")))
    return {str(v).strip() for v in values if str(v).strip()}

def _index_product(product):
    added = 0
    with CACHE_LOCK:
        for barcode in _barcode_values(product.get("barcodes")):
            PRODUCT_BARCODE_CACHE[str(barcode).strip()] = product
        for pack in product.get("packs", []) or []:
            for barcode in _pack_barcodes(pack):
                PACK_CACHE[barcode] = {"product": product, "pack": pack}
                added += 1
    return added

def _get_product_list(force=False):
    now = time.time()
    with CACHE_LOCK:
        if (
            not force
            and PRODUCT_LIST_CACHE["rows"]
            and now - PRODUCT_LIST_CACHE["ts"] < PRODUCT_LIST_TTL
        ):
            return PRODUCT_LIST_CACHE["rows"]

    rows_all = []
    limit = 1000
    offset = 0
    while True:
        r = requests.get(
            f"{API_BASE}/entity/product",
            headers=headers(),
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("rows", [])
        rows_all.extend(rows)
        for product in rows:
            _index_product(product)
        if len(rows) < limit:
            break
        offset += limit

    with CACHE_LOCK:
        PRODUCT_LIST_CACHE["rows"] = rows_all
        PRODUCT_LIST_CACHE["ts"] = now
    return rows_all

def _load_full_product(product):
    product_id = product.get("id")
    if not product_id:
        return product

    with CACHE_LOCK:
        if product_id in DETAILED_PRODUCT_IDS:
            return product

    r = requests.get(
        f"{API_BASE}/entity/product/{product_id}",
        headers=headers(),
        timeout=30,
    )
    r.raise_for_status()
    full_product = r.json()
    _index_product(full_product)

    with CACHE_LOCK:
        DETAILED_PRODUCT_IDS.add(product_id)
        WARMUP_STATE["indexed"] = len(PACK_CACHE)
    return full_product

def find_product_pack_by_barcode(barcode):
    target = str(barcode).strip()

    # Горячий путь: обычно один словарный lookup, без запроса к МойСклад.
    with CACHE_LOCK:
        found = PACK_CACHE.get(target)
    if found:
        return found["product"], found["pack"]

    rows = _get_product_list()

    # После загрузки списка код мог уже попасть в индекс.
    with CACHE_LOCK:
        found = PACK_CACHE.get(target)
    if found:
        return found["product"], found["pack"]

    # Только для еще не просмотренных карточек идем за полными данными.
    for product in rows:
        product_id = product.get("id")
        with CACHE_LOCK:
            already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
        if already_detailed:
            continue

        full_product = _load_full_product(product)
        with CACHE_LOCK:
            found = PACK_CACHE.get(target)
        if found:
            return found["product"], found["pack"]

    return None, None

def _cached_product_by_barcode(target):
    """Fast memory-only lookup for a unit or its packaging barcode."""
    with CACHE_LOCK:
        pack_found = PACK_CACHE.get(target)
        product_found = PRODUCT_BARCODE_CACHE.get(target)
    if pack_found:
        return pack_found["product"], pack_found["pack"]
    if product_found:
        return product_found, {"name": "ШК товара", "quantity": 1}
    return None, None


class FbsCatalogIndexPending(Exception):
    """A barcode may exist in a pack not yet loaded into the local index."""


def find_fbs_product_by_barcode(barcode):
    """FBS-specific bounded lookup: never scan every product card synchronously.

    The full-pack index remains in the background for receiving and unusual
    package barcodes. For a cold FBS barcode use one exact filtered API request
    rather than the former O(number of products) series of 30-second calls.
    """
    target = str(barcode).strip()
    product, pack = _cached_product_by_barcode(target)
    if product:
        return product, pack

    # After Render restarts, use the last verified PostgreSQL snapshot first.
    # Existing barcodes must not wait on MoySklad or packaging warm-up.
    try:
        product, pack = find_persisted_fbs_barcode(target)
    except Exception as exc:
        print(f"FBS persistent catalog read failed: {exc}", flush=True)
        product, pack = None, None
    if product:
        with CACHE_LOCK:
            if pack.get("name") == "Упаковка":
                PACK_CACHE[target] = {"product": product, "pack": pack}
            else:
                PRODUCT_BARCODE_CACHE[target] = product
        return product, pack

    response = requests.get(
        f"{API_BASE}/entity/product",
        headers=headers(),
        params={"filter": "barcode=" + target, "limit": 100},
        timeout=(3, 8),
    )
    response.raise_for_status()
    for candidate in response.json().get("rows", []):
        if isinstance(candidate, dict):
            _index_product(candidate)

    # A filtered result must be independently verified against the scanned
    # barcode; never accept an unrelated SKU just because API returned it.
    product, pack = _cached_product_by_barcode(target)
    if product:
        return product, pack

    # Package barcodes may only appear on the detailed product record.
    # Allow the background index to finish, but don't block a worker's scan
    # while fetching hundreds of individual products synchronously.
    start_pack_warmup()
    product, pack = _cached_product_by_barcode(target)
    if product:
        return product, pack
    with CACHE_LOCK:
        ready = bool(WARMUP_STATE["ready"])
        error = str(WARMUP_STATE["error"] or "")
    if not ready or error:
        raise FbsCatalogIndexPending(
            "ШК пока не найден в быстром справочнике. Идёт загрузка упаковок; "
            "повтори сканирование после завершения обновления каталога."
        )
    return None, None


def find_product_by_any_barcode(barcode):
    target = str(barcode).strip()

    with CACHE_LOCK:
        pack_found = PACK_CACHE.get(target)
        product_found = PRODUCT_BARCODE_CACHE.get(target)
    if pack_found:
        return pack_found["product"], pack_found["pack"]
    if product_found:
        return product_found, {"name": "ШК товара", "quantity": 1}

    rows = _get_product_list()

    with CACHE_LOCK:
        pack_found = PACK_CACHE.get(target)
        product_found = PRODUCT_BARCODE_CACHE.get(target)
    if pack_found:
        return pack_found["product"], pack_found["pack"]
    if product_found:
        return product_found, {"name": "ШК товара", "quantity": 1}

    for product in rows:
        product_id = product.get("id")
        with CACHE_LOCK:
            already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
        if already_detailed:
            continue

        _load_full_product(product)
        with CACHE_LOCK:
            pack_found = PACK_CACHE.get(target)
            product_found = PRODUCT_BARCODE_CACHE.get(target)
        if pack_found:
            return pack_found["product"], pack_found["pack"]
        if product_found:
            return product_found, {"name": "ШК товара", "quantity": 1}

    return None, None


def normalize_sscc(value):
    raw = str(value or "").strip().replace(" ", "")
    if raw.startswith("]C1"):
        raw = raw[3:]
    if raw.startswith("(00)"):
        raw = raw[4:]
    elif raw.startswith("00") and len(raw) == 20 and raw.isdigit():
        raw = raw[2:]

    if len(raw) != 18 or not raw.isdigit():
        raise ValueError("Это не SSCC ОСЗ: ожидается GS1 код (00) + 18 цифр.")

    body = raw[:-1]
    supplied = int(raw[-1])
    total = 0
    for index, digit in enumerate(reversed(body)):
        total += int(digit) * (3 if index % 2 == 0 else 1)
    expected = (10 - (total % 10)) % 10
    if supplied != expected:
        raise ValueError("SSCC считан с ошибкой: не сходится контрольная цифра.")

    return raw


def _warm_pack_cache_worker():
    try:
        with CACHE_LOCK:
            WARMUP_STATE.update({"running": True, "ready": False, "error": ""})

        rows = _get_product_list()

        # Прогреваем кеш спокойно, по одному товару.
        # Параллельный прогрев создавал всплеск запросов и мог выбить
        # лимит МойСклад, из-за чего обычные операции получали HTTP 429.
        for product in rows:
            product_id = product.get("id")
            with CACHE_LOCK:
                already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
            if already_detailed:
                continue

            try:
                _load_full_product(product)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    time.sleep(1.5)
                    try:
                        _load_full_product(product)
                    except Exception as retry_exc:
                        with CACHE_LOCK:
                            WARMUP_STATE["error"] = str(retry_exc)
                else:
                    with CACHE_LOCK:
                        WARMUP_STATE["error"] = str(exc)
            except Exception as exc:
                with CACHE_LOCK:
                    WARMUP_STATE["error"] = str(exc)

            # Не забиваем общий лимит API: приёмка важнее фонового прогрева.
            time.sleep(0.25)

        # Publish only a complete catalog; a failed MoySklad request must
        # never overwrite the previously working PostgreSQL snapshot.
        with CACHE_LOCK:
            snapshot_complete = not bool(WARMUP_STATE["error"])
            unit_codes = dict(PRODUCT_BARCODE_CACHE)
            pack_codes = dict(PACK_CACHE)
        if snapshot_complete:
            try:
                persisted = save_fbs_catalog_snapshot(unit_codes, pack_codes)
                print(f"FBS persisted catalog: {persisted} barcodes", flush=True)
            except Exception as exc:
                with CACHE_LOCK:
                    WARMUP_STATE["error"] = str(exc)
        with CACHE_LOCK:
            WARMUP_STATE["ready"] = not bool(WARMUP_STATE["error"])
            WARMUP_STATE["indexed"] = len(PACK_CACHE)
    except Exception as e:
        with CACHE_LOCK:
            WARMUP_STATE["error"] = str(e)
    finally:
        with CACHE_LOCK:
            WARMUP_STATE["running"] = False

def start_pack_warmup():
    with CACHE_LOCK:
        if WARMUP_STATE["running"] or WARMUP_STATE["ready"]:
            return
        WARMUP_STATE["running"] = True
    threading.Thread(target=_warm_pack_cache_worker, daemon=True).start()

class ReceiptUnavailable(Exception):
    def __init__(self, reason, name=""):
        self.reason = reason
        self.name = str(name or "").strip()
        super().__init__("Оприходование недоступно")


def receipt_is_deleted(doc):
    # Deleted entity representations differ depending on MoySklad API version.
    return any(doc.get(key) for key in ("deleted", "isDeleted", "deletedAt"))


def receipt_error_is_deleted(exc):
    response = getattr(exc, "response", None)
    if response is None:
        return False
    if response.status_code != 412:
        return False
    body = str(response.text or "").lower()
    return "корзин" in body or '"code": 3007' in body or '"code":3007' in body


def validate_receipt_document(doc):
    if receipt_is_deleted(doc):
        raise ReceiptUnavailable("deleted", doc.get("name"))
    if doc.get("applicable"):
        raise ReceiptUnavailable("completed", doc.get("name"))
    return doc


def receipt_unavailable_response(exc, fallback_name=""):
    name = exc.name or fallback_name or "Выбранное оприходование"
    if exc.reason == "completed":
        return jsonify(
            error=f"Оприходование №{name} уже проведено. Выберите другое или начните новую приёмку.",
            reason="completed", document_name=name,
        ), 409
    if exc.reason == "missing":
        return jsonify(
            error="Оприходование больше недоступно в МойСклад. Выберите другое или начните новую приёмку.",
            reason="missing", document_name=name,
        ), 409
    return jsonify(
        error=f"Оприходование №{name} находится в корзине МойСклад. Выберите другое оприходование или начните новую приёмку.",
        reason="deleted", document_name=name,
    ), 409


def get_receipt(receipt_id):
    r = requests.get(
        f"{API_BASE}/entity/enter/{receipt_id}",
        headers=headers(),
        params={"expand": "positions"},
        timeout=30,
    )
    r.raise_for_status()
    doc = r.json()
    validate_receipt_document(doc)
    return doc


@app.get("/api/receipts/<receipt_id>/status")
def receipt_document_status(receipt_id):
    """Read-only check before restoring/opening a stored document on the phone."""
    try:
        receipt = get_receipt(receipt_id)
        return jsonify(ok=True, id=receipt.get("id"), name=receipt.get("name"), status="draft")
    except ReceiptUnavailable as exc:
        return receipt_unavailable_response(exc)
    except requests.HTTPError as exc:
        if receipt_error_is_deleted(exc):
            return receipt_unavailable_response(ReceiptUnavailable("deleted"))
        if exc.response is not None and exc.response.status_code in (404, 410):
            return receipt_unavailable_response(ReceiptUnavailable("missing"))
        return jsonify(error="Не удалось проверить оприходование в МойСклад. Попробуйте обновить страницу.",
                       reason="temporary"), 502
    except Exception:
        return jsonify(error="Не удалось проверить доступность оприходования. Проверьте подключение.",
                       reason="temporary"), 503


def add_or_increment_position(receipt_id, product, pack, requested_qty=None, enforce_pack_limit=True):
    receipt = get_receipt(receipt_id)
    if receipt.get("applicable"):
        raise ValueError("Это Оприходование уже проведено. Открой черновик или создай новое.")

    positions = receipt.get("positions", {}).get("rows", []) or []
    product_href = product["meta"]["href"]
    standard_qty = float(pack["quantity"])
    add_qty = standard_qty if requested_qty is None else float(requested_qty)

    if add_qty <= 0:
        raise ValueError("Количество должно быть больше нуля.")
    if enforce_pack_limit and add_qty > standard_qty:
        raise ValueError(f"Количество {add_qty:g} больше стандартной палеты {standard_qty:g}.")

    for pos in positions:
        if pos.get("assortment", {}).get("meta", {}).get("href") == product_href:
            new_qty = float(pos.get("quantity", 0) or 0) + add_qty
            r = requests.put(
                f"{API_BASE}/entity/enter/{receipt_id}/positions/{pos['id']}",
                headers=headers(),
                json={"quantity": new_qty},
                timeout=30,
            )
            r.raise_for_status()
            return {"mode": "incremented", "added": add_qty, "total": new_qty}

    price = int(((product.get("buyPrice") or {}).get("value")) or 0)
    payload = {
        "quantity": add_qty,
        "price": price,
        "discount": 0,
        "vat": int(product.get("vat", 0) or 0),
        "assortment": {
            "meta": {
                "href": product_href,
                "type": product["meta"].get("type", "product"),
                "mediaType": "application/json",
            }
        },
    }

    r = requests.post(
        f"{API_BASE}/entity/enter/{receipt_id}/positions",
        headers=headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return {"mode": "created", "added": add_qty, "total": add_qty}

@app.get("/")
def index():
    return PAGE

def _system_status_payload():
    db_backend = "postgres" if os.getenv("DATABASE_URL", "").strip() else "sqlite"
    backup = backup_status()
    startup = _startup_snapshot()

    if not _WMS_READY.is_set():
        return {
            "ok": False,
            "startup_ready": False,
            "startup": startup,
            "token_configured": bool(TOKEN),
            "database_backend": db_backend,
            "database_ok": False,
            "database_error": startup.get("last_error") or "database restore/init in progress",
            "backup": backup,
        }

    db_ok = False
    db_error = ""
    try:
        row = query_one("SELECT 1")
        db_ok = bool(row and row[0] == 1)
    except Exception as e:
        db_error = str(e)

    return {
        "ok": bool(db_ok and TOKEN),
        "startup_ready": True,
        "startup": startup,
        "token_configured": bool(TOKEN),
        "database_backend": db_backend,
        "database_ok": db_ok,
        "database_error": db_error,
        "backup": backup,
    }

@app.get("/api/startup")
def startup_status():
    state = _startup_snapshot()
    return jsonify(
        ok=True,
        ready=_WMS_READY.is_set(),
        build=_build_id(),
        attempts=int(state.get("attempts", 0) or 0),
        last_attempt_at=state.get("last_attempt_at"),
        message=_startup_public_message(),
    )


@app.get("/api/health")
def health():
    # Keep HTTP 200 while restore is retrying so Render does not kill/restart
    # the service. "ready" tells us whether WMS itself can serve work yet.
    payload = _system_status_payload()
    if not payload.get("startup_ready"):
        return jsonify(ok=True, ready=False), 200
    return jsonify(ok=bool(payload["ok"]), ready=True), (200 if payload["ok"] else 503)


@app.get("/api/system/status")
def system_status():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    return jsonify(**_system_status_payload())

@app.get("/api/bootstrap")
def bootstrap():
    try:
        return jsonify(**bootstrap_data())
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/receipts")
def receipts():
    try:
        return jsonify(receipts=list_receipts())
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/receipts")
def new_receipt():
    try:
        data = request.get_json(force=True)
        state_id = str(data.get("state_id", "")).strip()
        organization_id = str(data.get("organization_id", "")).strip()
        store_id = str(data.get("store_id", "")).strip()

        if not state_id or not organization_id or not store_id:
            return jsonify(error="Нужно выбрать склад, поставщика/статус и организацию"), 400

        doc = create_receipt(state_id, organization_id, store_id)

        state_name = ""
        store_name = ""
        try:
            state_name = (doc.get("state") or {}).get("name") or ""
            store_name = (doc.get("store") or {}).get("name") or ""
        except Exception:
            pass

        employee = get_session_employee()
        if employee:
            log_event(
                employee["id"],
                "receiving",
                "create",
                object_code=doc.get("id"),
                result=state_name or "",
                document_name=doc.get("name") or "Новое Оприходование",
            )

        return jsonify(
            ok=True,
            id=doc.get("id"),
            name=doc.get("name") or "Новое Оприходование",
            state=state_name or "Статус выбран",
            store=store_name or "Склад выбран",
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/receipts/<receipt_id>/complete")
def finish_receipt(receipt_id):
    try:
        if has_unresolved_operations(receipt_id):
            return jsonify(error="Есть неподтверждённая операция приёмки. Сначала сверь документ МойСклад."), 409
        # Сводку считаем по самому документу МойСклад, чтобы финальный экран
        # не зависел от локальных счётчиков телефона.
        before = get_receipt(receipt_id)
        positions = (before.get("positions") or {}).get("rows", []) or []
        positions_count = len(positions)
        total_units = sum(float(x.get("quantity", 0) or 0) for x in positions)

        scan_row = query_one(
            """
            SELECT COUNT(*)
            FROM operation_events
            WHERE operation_type = ? AND event_type IN (?, ?) AND result = ?
            """,
            ("receiving", "scan", "external_pallet", receipt_id),
        )
        pallets_count = int(scan_row[0]) if scan_row and scan_row[0] is not None else 0

        doc = complete_receipt(receipt_id)
        employee = get_session_employee()
        if employee:
            log_event(
                employee["id"],
                "receiving",
                "complete",
                object_code=receipt_id,
                quantity=total_units,
                result=doc.get("name") or receipt_id,
                document_name=doc.get("name") or receipt_id,
            )

        # После завершения приёмки сразу делаем аварийную копию WMS,
        # но не задерживаем ответ сотруднику ожиданием Object Storage.
        if not os.getenv("DATABASE_URL", "").strip():
            threading.Thread(target=backup_now, kwargs={"force": True}, daemon=True).start()

        # Целые количества показываем без .0.
        total_units_out = int(total_units) if float(total_units).is_integer() else total_units
        return jsonify(
            ok=True,
            id=doc.get("id"),
            name=doc.get("name") or "Оприходование",
            positions=positions_count,
            total_units=total_units_out,
            pallets=pallets_count,
            employee=(employee or {}).get("name", ""),
        )
    except ReceiptUnavailable as exc:
        return receipt_unavailable_response(exc)
    except requests.HTTPError as e:
        if receipt_error_is_deleted(e):
            return receipt_unavailable_response(ReceiptUnavailable("deleted", before.get("name", "") if "before" in locals() else ""))
        if e.response is not None and e.response.status_code in (404, 410):
            return receipt_unavailable_response(ReceiptUnavailable("missing"))
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

def osz_receiving_operation_id(sscc):
    """Stable receiving ID for one physical OSZ SSCC across devices and retries."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "onix:receiving:osz:" + sscc))


@app.post("/api/osz/check")
def osz_check():
    try:
        data = request.get_json(force=True)
        sscc = normalize_sscc(data.get("sscc", ""))
        existing = query_one(
            "SELECT receipt_id, article, quantity, accepted_at FROM external_pallets WHERE sscc = ?",
            (sscc,),
        )
        if existing:
            return jsonify(
                error=f"Палета ОСЗ SSCC {sscc} уже была принята ранее.",
                receipt_id=existing[0],
                article=existing[1],
                quantity=existing[2],
                accepted_at=existing[3],
            ), 409
        operation = read_operation(osz_receiving_operation_id(sscc))
        if operation:
            return jsonify(
                error="Эта палета ОСЗ уже отправлялась на приёмку. Проверь МойСклад перед повторной работой.",
                status=operation["status"],
                operation_id=operation["operation_id"],
            ), 409
        return jsonify(ok=True, sscc=sscc)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/fbs/product-info")
def fbs_product_info():
    started = time.perf_counter()

    @after_this_request
    def add_scan_timing(response):
        response.headers["X-ONIX-Server-Ms"] = f"{(time.perf_counter() - started) * 1000:.1f}"
        response.headers["Cache-Control"] = "no-store"
        return response

    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой ШК товара"), 400

        product, pack = find_fbs_product_by_barcode(barcode)
        if not product:
            employee = get_session_employee()
            if employee:
                log_event(
                    employee["id"],
                    "fbs",
                    "product_not_found",
                    object_code=barcode,
                    result="not-found",
                )
            return jsonify(error=f"Товар со штрихкодом {barcode} не найден"), 404

        qty = float(pack.get("quantity") or 1)
        if qty > 1:
            qty_out = int(qty) if qty.is_integer() else qty
            return jsonify(
                error=f"Считан ШК упаковки на {qty_out} шт. Для FBS нужен ШК одной единицы товара."
            ), 409

        return jsonify(
            ok=True,
            barcode=barcode,
            article=product.get("article"),
            name=product.get("name"),
        )
    except FbsCatalogIndexPending as e:
        return jsonify(error=str(e), retryable=True, reason="catalog_indexing"), 503
    except requests.HTTPError as e:
        body = e.response.text[:800] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except requests.Timeout:
        return jsonify(error="МойСклад долго отвечает. Проверь связь и повтори ШК товара."), 504
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/osz/product-info")
def osz_product_info():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой ШК товара"), 400

        product, pack = find_product_by_any_barcode(barcode)
        if not product:
            return jsonify(error=f"Товар со штрихкодом {barcode} не найден"), 404

        standard_qty = float(pack.get("quantity") or 0)
        standard_qty_out = int(standard_qty) if standard_qty.is_integer() else standard_qty
        return jsonify(
            ok=True,
            barcode=barcode,
            name=product.get("name"),
            article=product.get("article"),
            pack_name=pack.get("name"),
            standard_qty=standard_qty_out,
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/osz/accept")
def osz_accept():
    """Receive an OSZ SSCC once, even if several devices submit it simultaneously.

    The durable confirmation is reserved BEFORE calling MoySklad. Uncertain
    writes are never retried automatically; the warehouse must reconcile them.
    """
    try:
        data = request.get_json(force=True)
        receipt_id = str(data.get("receipt_id", "")).strip()
        sscc = normalize_sscc(data.get("sscc", ""))
        product_barcode = str(data.get("product_barcode", "")).strip()
        raw_quantity = data.get("quantity")
        if isinstance(raw_quantity, bool):
            return jsonify(error="Количество должно быть положительным числом"), 400
        quantity = float(raw_quantity)
        if not math.isfinite(quantity) or quantity <= 0 or quantity > 10000000:
            return jsonify(error="Некорректное количество палеты ОСЗ"), 400
        if not receipt_id:
            return jsonify(error="Не выбрано Оприходование"), 400
        if not product_barcode:
            return jsonify(error="Не отсканирован товар"), 400

        employee = get_session_employee()
        if not employee:
            return jsonify(error="Нужен вход по бейджу"), 401

        accepted = query_one("SELECT receipt_id FROM external_pallets WHERE sscc = ?", (sscc,))
        if accepted:
            return jsonify(error=f"Палета ОСЗ SSCC {sscc} уже принята."), 409

        # SSCC, not browser state, is the stable idempotency key. This blocks
        # double taps, parallel devices, different employees and changed UUIDs.
        operation_id = osz_receiving_operation_id(sscc)
        previous = read_operation(operation_id)
        if previous:
            return jsonify(
                error="Эта палета ОСЗ уже передавалась в МойСклад. Проверь документ и статус операции.",
                operation_id=operation_id,
                status=previous["status"],
            ), 409

        product, pack = find_product_by_any_barcode(product_barcode)
        if not product:
            return jsonify(error=f"Товар со штрихкодом {product_barcode} не найден"), 404

        # Atomic INSERT ON CONFLICT: only one request can call the external API.
        try:
            claimed, previous = claim_operation(
                operation_id, receipt_id, employee["id"], sscc, str(quantity)
            )
        except ValueError:
            return jsonify(error="Палета ОСЗ уже закреплена за другим документом или сотрудником",
                           operation_id=operation_id), 409
        if not claimed:
            return jsonify(
                error="Палета ОСЗ уже обрабатывается или ожидает сверки с МойСклад.",
                operation_id=operation_id,
                status=previous["status"],
            ), 409

        try:
            result = add_or_increment_position(
                receipt_id, product, pack, requested_qty=quantity, enforce_pack_limit=False
            )

            # Persist the accepted SSCC before completing the confirmation.
            # Any failure after the MoySklad write leaves an unresolved operation.
            execute(
                """
                INSERT INTO external_pallets
                (sscc, partner, receipt_id, employee_id, product_barcode, article, quantity, accepted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sscc, "OSZ", receipt_id, employee["id"], product_barcode,
                 product.get("article"), result["added"], now_iso()),
            )
            response = dict(
                sscc=sscc,
                barcode=product_barcode,
                name=product.get("name"),
                article=product.get("article"),
                added=int(result["added"]) if float(result["added"]).is_integer() else result["added"],
                total=int(result["total"]) if float(result["total"]).is_integer() else result["total"],
            )
            finish_operation(operation_id, response)
        except ReceiptUnavailable as exc:
            # The document is known unavailable before an external write.
            reject_operation(operation_id, exc.reason)
            return receipt_unavailable_response(exc)
        except requests.HTTPError as exc:
            if receipt_error_is_deleted(exc):
                reject_operation(operation_id, "deleted")
                return receipt_unavailable_response(ReceiptUnavailable("deleted"))
            if exc.response is not None and exc.response.status_code in (404, 410):
                reject_operation(operation_id, "missing")
                return receipt_unavailable_response(ReceiptUnavailable("missing"))
            mark_uncertain(operation_id, str(exc))
            return jsonify(error="Результат приёмки ОСЗ не подтверждён. Сверь документ МойСклад перед повторной работой.",
                           operation_id=operation_id, status="uncertain"), 503
        except Exception as exc:
            mark_uncertain(operation_id, str(exc))
            return jsonify(error="Приёмка ОСЗ требует сверки с МойСклад. Не сканируй эту палету повторно.",
                           operation_id=operation_id, status="uncertain"), 503

        # Audit is best-effort: failure must not turn a confirmed write into
        # a retryable HTTP error that causes another employee to re-add stock.
        try:
            log_event(
                employee["id"], "receiving", "external_pallet", object_code=sscc,
                quantity=result["added"], result=receipt_id,
                article=product.get("article"), product_name=product.get("name"),
            )
        except Exception as exc:
            print(f"OSZ receiving audit failed {operation_id}: {exc}", flush=True)

        return jsonify(ok=True, operation_id=operation_id, **response)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="Не удалось проверить приёмку ОСЗ. Повтори проверку статуса документа, не скан палеты."), 503


@app.post("/api/warmup")
def warmup():
    try:
        start_pack_warmup()
        with CACHE_LOCK:
            state = dict(WARMUP_STATE)
            state["cached_barcodes"] = len(PACK_CACHE)
        return jsonify(ok=True, **state)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/cache-status")
def cache_status():
    with CACHE_LOCK:
        return jsonify(
            ok=True,
            cached_barcodes=len(PACK_CACHE),
            detailed_products=len(DETAILED_PRODUCT_IDS),
            **fbs_catalog_status(),
            **WARMUP_STATE,
        )

def partner_pallet_status_error(pallet):
    status = pallet["status"]
    if status == "accepted":
        accepted_at = pallet.get("accepted_at")
        if accepted_at:
            try:
                local_dt = datetime.fromisoformat(accepted_at.replace("Z", "+00:00")) + timedelta(hours=3)
                return "Палета уже принята: " + local_dt.strftime("%d.%m.%Y в %H:%M") + " (МСК)"
            except ValueError:
                pass
        return "Палета уже принята (время старой приёмки не записано)"
    if status == "processing":
        return "Палета сейчас в обработке"
    if status == "uncertain":
        return "Приёмка палеты требует сверки с МойСклад"
    return "Палета недоступна для приёмки"


def partner_receiving_product(line):
    """Resolve a pre-advised SKU against MoySklad without touching its inventory."""
    article = line["article"]
    matching = [p for p in _get_product_list() if str(p.get("article", "")).strip() == article]
    if len(matching) != 1:
        raise ValueError("Артикул палеты отсутствует или неоднозначен в МойСклад: " + article)
    return matching[0], {"name": "Палета партнёра", "quantity": line["quantity"]}


@app.post("/api/pack-info")
def pack_info():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400

        if barcode.startswith("ONIX-PAL-"):
            pallet = resolve_receiving_pallet(barcode)
            if not pallet:
                return jsonify(error="ШК палеты партнёра не найден в ONIX"), 404
            if pallet["status"] != "created":
                return jsonify(error=partner_pallet_status_error(pallet)), 409
            if len(pallet["lines"]) != 1:
                return jsonify(error="Многономенклатурная палета требует отдельной проверки"), 409
            product, pack = partner_receiving_product(pallet["lines"][0])
            return jsonify(ok=True, barcode=barcode, name=product.get("name"),
                           article=product.get("article"), pack_name="Партнёр: "+pallet["partner"],
                           standard_qty=pack["quantity"], source="partner")
        product, pack = find_product_pack_by_barcode(barcode)
        if not product:
            return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        return jsonify(
            ok=True,
            barcode=barcode,
            name=product.get("name"),
            article=product.get("article"),
            pack_name=pack.get("name"),
            standard_qty=float(pack.get("quantity") or 0),
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/scan-operation/<operation_id>")
def receiving_scan_operation_status(operation_id):
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Нужен вход по бейджу"), 401
    try:
        operation_id = str(uuid.UUID(operation_id))
    except ValueError:
        return jsonify(error="Неверный номер операции"), 400
    record = read_operation(operation_id)
    if not record or record["employee_id"] != employee["id"]:
        return jsonify(error="Операция не найдена"), 404
    return jsonify(ok=record["status"] == "done",
                   status=record["status"],
                   receipt_id=record["receipt_id"],
                   reason=record["error"] if record["status"] == "rejected" else None,
                   response=record["response"] if record["status"] == "done" else None)


@app.post("/api/scan-operation/<operation_id>/reconcile")
def manually_reconcile_receiving_scan(operation_id):
    """After a MoySklad document check, close an uncertain operation, not retry it."""
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Нужен вход по бейджу"), 401
    try:
        operation_id = str(uuid.UUID(operation_id))
    except ValueError:
        return jsonify(error="Неверный номер операции"), 400
    record = read_operation(operation_id)
    if not record or record["employee_id"] != employee["id"]:
        return jsonify(error="Операция не найдена"), 404
    if not (request.get_json(silent=True) or {}).get("verified"):
        return jsonify(error="Необходимо подтвердить сверку с МойСклад"), 400
    if record["status"] == "done":
        return jsonify(ok=True, status="done", response=record["response"])
    if record["status"] == "processing":
        try:
            started = datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - started < timedelta(minutes=2):
                return jsonify(error="Операция ещё обрабатывается. Повтори проверку через 2 минуты."), 409
        except (TypeError, ValueError):
            return jsonify(error="Невозможно проверить время операции"), 409
    if record["status"] not in ("processing", "uncertain"):
        return jsonify(error="Операция уже закрыта"), 409
    updated = reconcile_after_manual_check(operation_id)
    if updated["status"] == "done":
        return jsonify(ok=True, status="done", response=updated["response"])
    return jsonify(ok=True, status=updated["status"])


@app.post("/api/scan")
def scan():
    """Confirmed pallet acceptance. Exactly one MoySklad write attempt per UUID.

    A crash or timeout after MoySklad write leaves status processing/uncertain.
    Never retry such an operation automatically; it requires reconciliation.
    """
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        receipt_id = str(data.get("receipt_id", "")).strip()
        employee = get_session_employee()
        if not employee:
            return jsonify(error="Нужен вход по бейджу"), 401
        try:
            operation_id = str(uuid.UUID(str(data.get("operation_id", "")).strip()))
        except ValueError:
            return jsonify(error="Обнови страницу приёмки: для подтверждения нужен номер операции"), 400
        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400
        if not receipt_id:
            return jsonify(error="Не выбрано Оприходование"), 400
        requested_qty = data.get("quantity", None)
        if requested_qty is not None:
            requested_qty = float(requested_qty)
            if not math.isfinite(requested_qty) or requested_qty <= 0:
                return jsonify(error="Некорректное количество"), 400
        qty_key = "" if requested_qty is None else str(requested_qty)

        # Return an already processed operation BEFORE looking up MoySklad.
        existing = read_operation(operation_id)
        if existing:
            if (existing["receipt_id"] != receipt_id or existing["barcode"] != barcode
                    or existing["employee_id"] != employee["id"]
                    or existing["requested_qty"] != qty_key):
                return jsonify(error="Номер операции уже относится к другой палете"), 409
            if existing["status"] == "done":
                return jsonify(ok=True, operation_id=operation_id, replayed=True, **existing["response"])
            return jsonify(error="Операция уже отправлена. Проверь результат в МойСклад; повторно товар не добавлялся.",
                           operation_id=operation_id, status=existing["status"]), 409

        partner_pallet = None
        if barcode.startswith("ONIX-PAL-"):
            partner_pallet = resolve_receiving_pallet(barcode)
            if not partner_pallet:
                return jsonify(error="ШК палеты партнёра не найден в ONIX"), 404
            if partner_pallet["status"] != "created":
                return jsonify(error=partner_pallet_status_error(partner_pallet)), 409
            if len(partner_pallet["lines"]) != 1:
                return jsonify(error="Палета с несколькими SKU требует отдельной приёмки"), 409
            product, pack = partner_receiving_product(partner_pallet["lines"][0])
            if requested_qty is not None and requested_qty != float(pack["quantity"]):
                return jsonify(error="Количество по палете партнёра нельзя менять в этом режиме"), 409
        else:
            product, pack = find_product_pack_by_barcode(barcode)
            if not product:
                return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        # Atomic claim blocks concurrent requests, double-taps, and HTTP retries.
        claimed, record = claim_operation(
            operation_id, receipt_id, employee["id"], barcode, qty_key
        )
        if not claimed:
            if record["status"] == "done":
                return jsonify(ok=True, operation_id=operation_id, replayed=True, **record["response"])
            return jsonify(error="Операция обрабатывается или требует сверки.",
                           operation_id=operation_id, status=record["status"]), 409

        if partner_pallet and not claim_receiving_pallet(partner_pallet["id"]):
            reject_operation(operation_id, "duplicate_partner_pallet")
            return jsonify(error="Эта палета уже принята или обрабатывается"), 409

        try:
            result = add_or_increment_position(receipt_id, product, pack, requested_qty)
            response = dict(
                barcode=barcode,
                name=product.get("name"),
                article=product.get("article"),
                pack_name=pack.get("name"),
                added=result["added"],
                total=result["total"],
                mode=result["mode"],
            )
            # Commit the idempotency result FIRST; logging must not turn a
            # successful MoySklad write into a retryable error.
            finish_operation(operation_id, response)
            if partner_pallet:
                set_receiving_pallet_status(partner_pallet["id"], "accepted")
        except ReceiptUnavailable as exc:
            if partner_pallet:
                set_receiving_pallet_status(partner_pallet["id"], "created")
            reject_operation(operation_id, exc.reason)
            raise
        except requests.HTTPError as exc:
            if partner_pallet:
                set_receiving_pallet_status(partner_pallet["id"], "uncertain")
            if receipt_error_is_deleted(exc):
                reject_operation(operation_id, "deleted")
            elif exc.response is not None and exc.response.status_code in (404, 410):
                reject_operation(operation_id, "missing")
            else:
                mark_uncertain(operation_id, str(exc))
            raise
        except Exception as exc:
            if partner_pallet:
                set_receiving_pallet_status(partner_pallet["id"], "uncertain")
            mark_uncertain(operation_id, str(exc))
            raise

        try:
            log_event(
                employee["id"],
                "receiving",
                "scan",
                object_code=barcode,
                quantity=result["added"],
                result=receipt_id,
                article=product.get("article"),
                product_name=product.get("name"),
            )
        except Exception as exc:
            print(f"Receiving audit event failed for {operation_id}: {exc}", flush=True)

        return jsonify(ok=True, operation_id=operation_id, **response)
    except ReceiptUnavailable as e:
        return receipt_unavailable_response(e)
    except ValueError as e:
        return jsonify(error=str(e)), 409
    except requests.HTTPError as e:
        if receipt_error_is_deleted(e):
            return receipt_unavailable_response(ReceiptUnavailable("deleted"))
        if e.response is not None and e.response.status_code in (404, 410):
            return receipt_unavailable_response(ReceiptUnavailable("missing"))
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Запись в МойСклад не подтверждена: {e}. {body}. Проверь документ перед новым сканом.",
                       status="uncertain"), 503
    except Exception as e:
        return jsonify(error=f"Результат операции не подтверждён: {e}. Не сканируй эту палету повторно до сверки.",
                       status="uncertain"), 503

if __name__ == "__main__":
    _start_wms_initializer()
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
