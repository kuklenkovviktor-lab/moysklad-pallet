"""WB FBS Sync v1.1: read-only WB synchronization and local product -> sticker verification.

Feature is intentionally opt-in. No WB requests are made on barcode scans.
The caller must supply the already-scanned product barcode before the WB sticker.
"""
import json
import os
import random
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone

import requests

from wms import db_connect, sql

MARKETPLACE_API = "https://marketplace-api.wildberries.ru"
CONTENT_API = "https://content-api.wildberries.ru"
PICKABLE_STATUSES = frozenset({"confirm", "complete"})
_SYNC_LOCK = threading.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def init_schema():
    """Additive schema only. Does not alter existing GRIT tables."""
    schema = (
        """CREATE TABLE IF NOT EXISTS wb_fbs_orders (
            account_id TEXT NOT NULL, order_id TEXT NOT NULL, rid TEXT,
            nm_id TEXT, chrt_id TEXT, seller_article TEXT, warehouse_id TEXT,
            wb_status TEXT NOT NULL DEFAULT 'unknown',
            local_status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT, synced_at TEXT NOT NULL,
            PRIMARY KEY(account_id, order_id))""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_order_skus (
            account_id TEXT NOT NULL, order_id TEXT NOT NULL, sku TEXT NOT NULL,
            PRIMARY KEY(account_id, order_id, sku))""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_catalog_variants (
            account_id TEXT NOT NULL, nm_id TEXT NOT NULL,
            chrt_id TEXT NOT NULL, sku TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(account_id,nm_id,chrt_id,sku))""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_stickers (
            account_id TEXT NOT NULL, order_id TEXT NOT NULL,
            barcode TEXT NOT NULL, part_a TEXT, part_b TEXT,
            format TEXT, fetched_at TEXT NOT NULL,
            PRIMARY KEY(account_id, order_id))""",
        """CREATE INDEX IF NOT EXISTS idx_wb_fbs_sticker_lookup
            ON wb_fbs_stickers(account_id, barcode)""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_scan_events (
            event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, order_id TEXT,
            employee_id TEXT NOT NULL, terminal_id TEXT NOT NULL,
            expected_skus TEXT NOT NULL, scanned_barcode TEXT NOT NULL,
            scanned_sticker TEXT NOT NULL, result TEXT NOT NULL,
            reason TEXT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_sync_runs (
            run_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            started_at TEXT NOT NULL, finished_at TEXT, sync_type TEXT,
            status TEXT NOT NULL, received_count INTEGER DEFAULT 0,
            updated_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0,
            request_id TEXT, error_message TEXT)""",
    )
    with db_connect() as conn:
        cur = conn.cursor()
        for stmt in schema:
            cur.execute(stmt)
        conn.commit()
        cur.close()


def _clean(value):
    return str(value).strip() if value is not None else ""


def ingest_orders(account_id, orders, statuses=None):
    """UPSERT order snapshot. Never guess status or replace known SKUs with empties."""
    statuses = statuses or {}
    now = utcnow()
    updated = 0
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            for item in orders:
                oid = _clean(item.get("id"))
                if not oid:
                    continue
                nm = _clean(item.get("nmId"))
                chrt = _clean(item.get("chrtId"))
                skus = sorted({_clean(s) for s in (item.get("skus") or []) if _clean(s)})
                if not skus and nm and chrt:
                    cur.execute(sql("""SELECT sku FROM wb_fbs_catalog_variants
                        WHERE account_id=? AND nm_id=? AND chrt_id=?"""),
                        (account_id, nm, chrt))
                    skus = sorted(x[0] for x in cur.fetchall())
                status = _clean(statuses.get(oid) or item.get("wbStatus") or item.get("status") or "unknown")
                if status not in PICKABLE_STATUSES:
                    if status not in {"new", "cancel", "canceled", "cancelled", "decline", "unknown"}:
                        status = "unknown"
                cur.execute(sql("""INSERT INTO wb_fbs_orders
                    (account_id,order_id,rid,nm_id,chrt_id,seller_article,warehouse_id,
                     wb_status,local_status,created_at,synced_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,order_id) DO UPDATE SET
                      rid=excluded.rid, nm_id=excluded.nm_id, chrt_id=excluded.chrt_id,
                      seller_article=excluded.seller_article, warehouse_id=excluded.warehouse_id,
                      wb_status=excluded.wb_status, created_at=excluded.created_at,
                      synced_at=excluded.synced_at"""),
                    (account_id, oid, _clean(item.get("rid")),
                     nm, chrt, _clean(item.get("article")), _clean(item.get("warehouseId")),
                     status, "pending", _clean(item.get("createdAt")), now))
                if skus:
                    cur.execute(sql("DELETE FROM wb_fbs_order_skus WHERE account_id=? AND order_id=?"),
                                (account_id, oid))
                    for sku in skus:
                        cur.execute(sql("""INSERT INTO wb_fbs_order_skus
                            (account_id,order_id,sku) VALUES(?,?,?)
                            ON CONFLICT(account_id,order_id,sku) DO NOTHING"""),
                            (account_id, oid, sku))
                updated += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return updated


def ingest_catalog(account_id, cards):
    """Store exact WB nmId + chrtId -> SKU variants for offline fallback."""
    updated = 0
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            for card in cards:
                nm = _clean(card.get("nmID"))
                if not nm:
                    continue
                for variant in card.get("sizes") or []:
                    chrt = _clean(variant.get("chrtID"))
                    if not chrt:
                        continue
                    skus = sorted({_clean(s) for s in (variant.get("skus") or []) if _clean(s)})
                    cur.execute(sql("""DELETE FROM wb_fbs_catalog_variants
                        WHERE account_id=? AND nm_id=? AND chrt_id=?"""), (account_id, nm, chrt))
                    for sku in skus:
                        cur.execute(sql("""INSERT INTO wb_fbs_catalog_variants
                            (account_id,nm_id,chrt_id,sku,updated_at) VALUES(?,?,?,?,?)"""),
                            (account_id, nm, chrt, sku, utcnow()))
                    updated += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return updated


def ingest_stickers(account_id, stickers, fmt="png"):
    now = utcnow()
    updated = 0
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            for sticker in stickers:
                order_id = _clean(sticker.get("orderId"))
                barcode = _clean(sticker.get("barcode"))
                if not order_id or not barcode:
                    continue
                cur.execute(sql("""SELECT 1 FROM wb_fbs_orders
                        WHERE account_id=? AND order_id=?"""), (account_id, order_id))
                if not cur.fetchone():
                    continue
                cur.execute(sql("""INSERT INTO wb_fbs_stickers
                    (account_id,order_id,barcode,part_a,part_b,format,fetched_at)
                    VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(account_id,order_id) DO UPDATE SET
                    barcode=excluded.barcode,part_a=excluded.part_a,
                    part_b=excluded.part_b,format=excluded.format,fetched_at=excluded.fetched_at"""),
                    (account_id, order_id, barcode, _clean(sticker.get("partA")),
                     _clean(sticker.get("partB")), fmt, now))
                updated += 1
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return updated


def verify_pair(account_id, product_barcode, sticker_barcode):
    """Pure local lookup, no network. Returns result, order ID and authorized SKUs."""
    product = _clean(product_barcode)
    sticker = _clean(sticker_barcode)
    if not product:
        return {"result": "PRODUCT_REQUIRED", "order_id": None, "expected_skus": []}
    if not sticker:
        return {"result": "STICKER_REQUIRED", "order_id": None, "expected_skus": []}
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql("""SELECT o.order_id,o.wb_status,o.local_status
            FROM wb_fbs_stickers s JOIN wb_fbs_orders o
            ON o.account_id=s.account_id AND o.order_id=s.order_id
            WHERE s.account_id=? AND s.barcode=?"""), (account_id, sticker))
        matches = cur.fetchall()
        if len(matches) != 1:
            cur.close()
            return {"result": "AMBIGUOUS_STICKER" if matches else "UNKNOWN_STICKER",
                    "order_id": None, "expected_skus": []}
        oid, status, local_status = matches[0]
        cur.execute(sql("""SELECT sku FROM wb_fbs_order_skus
            WHERE account_id=? AND order_id=?"""), (account_id, oid))
        allowed = sorted(x[0] for x in cur.fetchall())
        cur.close()
    if local_status == "complete":
        result = "ALREADY_COMPLETE"
    elif status not in PICKABLE_STATUSES:
        result = "BLOCKED_STATUS"
    elif not allowed:
        result = "MAPPING_ERROR"
    elif product not in allowed:
        result = "MISMATCH"
    else:
        result = "MATCH"
    return {"result": result, "order_id": oid, "expected_skus": allowed}


def log_verification(account_id, employee_id, terminal_id, product, sticker, verdict):
    """Write both accepted and rejected scan attempts, no personal data from WB."""
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql("""INSERT INTO wb_fbs_scan_events
            (event_id,account_id,order_id,employee_id,terminal_id,expected_skus,
             scanned_barcode,scanned_sticker,result,reason,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)"""),
            (str(uuid.uuid4()), account_id, verdict.get("order_id"), employee_id,
             terminal_id, json.dumps(verdict.get("expected_skus", [])),
             _clean(product), _clean(sticker), verdict["result"],
             verdict.get("reason"), utcnow()))
        conn.commit()
        cur.close()


class WBClient:
    def __init__(self, marketplace_token, content_token=None, session=None):
        if not marketplace_token:
            raise ValueError("Marketplace API token missing")
        self.marketplace_token = marketplace_token
        self.content_token = content_token
        self.http = session or requests.Session()

    def request(self, method, base, path, token, **kwargs):
        last = None
        for attempt in range(4):
            try:
                response = self.http.request(
                    method, base + path,
                    headers={"Authorization": token, "Content-Type": "application/json"},
                    timeout=30, **kwargs)
                if response.status_code in (401, 403):
                    response.raise_for_status()
                if response.status_code == 429 or response.status_code >= 500:
                    last = requests.HTTPError(f"WB HTTP {response.status_code}")
                    if attempt == 3:
                        raise last
                    raw = response.headers.get("Retry-After", "0")
                    try:
                        delay = min(60.0, max(float(raw), 0.0))
                    except ValueError:
                        delay = 0.0
                    time.sleep(max(delay, 0.3 * (2 ** attempt)) + random.random() * 0.2)
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.ConnectionError, requests.Timeout) as exc:
                last = exc
                if attempt == 3:
                    raise
                time.sleep(0.3 * (2 ** attempt) + random.random() * 0.2)
        raise last

    def cards(self):
        if not self.content_token:
            raise ValueError("WB Content API token missing")
        cursor = {"limit": 100}
        seen = set()
        all_cards = []
        while True:
            payload = self.request("POST", CONTENT_API, "/content/v2/get/cards/list",
                                   self.content_token,
                                   json={"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}})
            cards = payload.get("cards") or []
            all_cards.extend(cards)
            next_data = payload.get("cursor") or {}
            pair = (str(next_data.get("updatedAt") or ""), str(next_data.get("nmID") or ""))
            if len(cards) < 100 or not all(pair) or pair in seen:
                break
            seen.add(pair)
            cursor = {"limit": 100, "updatedAt": pair[0], "nmID": next_data.get("nmID")}
        return all_cards

    def new_orders(self):
        return self.request("GET", MARKETPLACE_API, "/api/v3/orders/new",
                            self.marketplace_token).get("orders", [])

    def orders(self, date_from=None):
        cursor = 0
        seen = set()
        all_orders = []
        while True:
            params = {"limit": 1000, "next": cursor}
            if date_from is not None:
                params["dateFrom"] = date_from
            payload = self.request("GET", MARKETPLACE_API, "/api/v3/orders",
                                   self.marketplace_token, params=params)
            rows = payload.get("orders") or []
            all_orders.extend(rows)
            next_cursor = payload.get("next")
            if not rows or next_cursor in (None, 0, "0", cursor) or str(next_cursor) in seen:
                break
            seen.add(str(next_cursor))
            cursor = next_cursor
        return all_orders

    def statuses(self, ids):
        result = {}
        for offset in range(0, len(ids), 1000):
            data = self.request("POST", MARKETPLACE_API, "/api/v3/orders/status",
                                self.marketplace_token,
                                json={"orders": [int(x) for x in ids[offset:offset + 1000]]})
            for item in data.get("orders") or []:
                result[_clean(item.get("id"))] = _clean(item.get("supplierStatus"))
        return result

    def stickers(self, ids):
        result = []
        for offset in range(0, len(ids), 100):
            data = self.request("POST", MARKETPLACE_API,
                                "/api/v3/orders/stickers", self.marketplace_token,
                                params={"type": "png", "width": 58, "height": 40},
                                json={"orders": [int(x) for x in ids[offset:offset + 100]]})
            result.extend(data.get("stickers") or [])
        return result


def sync_once(account_id, client, date_from=None):
    """One synchronous read-only run; caller schedules; no concurrent workers in this process."""
    if not _SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("WB sync already running")
    run_id = str(uuid.uuid4())
    received = updated = 0
    try:
        init_schema()
        from wms import execute
        execute("""INSERT INTO wb_fbs_sync_runs
            (run_id,account_id,started_at,sync_type,status)
            VALUES(?,?,?,?,?)""", (run_id, account_id, utcnow(), "incremental", "running"))
        try:
            rows = {str(x["id"]): x for x in client.orders(date_from) if x.get("id") is not None}
            for item in client.new_orders():
                if item.get("id") is not None:
                    rows[str(item["id"])] = item
            received = len(rows)
            statuses = client.statuses(list(rows)) if rows else {}
            updated = ingest_orders(account_id, rows.values(), statuses)
            wanted = [int(oid) for oid, status in statuses.items()
                      if status in PICKABLE_STATUSES]
            if wanted:
                ingest_stickers(account_id, client.stickers(wanted))
            execute("""UPDATE wb_fbs_sync_runs SET finished_at=?,status='ok',
                received_count=?,updated_count=? WHERE run_id=?""",
                (utcnow(), received, updated, run_id))
        except Exception as exc:
            execute("""UPDATE wb_fbs_sync_runs SET finished_at=?,status='error',
                received_count=?,updated_count=?,error_count=1,error_message=?
                WHERE run_id=?""",
                (utcnow(), received, updated, type(exc).__name__, run_id))
            raise
        return {"run_id": run_id, "received": received, "updated": updated}
    finally:
        _SYNC_LOCK.release()


# Manager-facing control is explicitly disabled until the opt-in flag is set.
from flask import Blueprint, Response, jsonify, request, session
from wms import manager_access, query_one

wb_fbs_v11_bp = Blueprint("wb_fbs_v11", __name__)
_UI_STATE = {"running": False, "last_error": "", "last_result": None}


def _allowed():
    return os.getenv("WB_FBS_V11_ENABLED", "0") == "1"


def _account():
    return os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"


def _manual_sync():
    _UI_STATE["running"] = True
    try:
        client = WBClient(os.getenv("WB_API_TOKEN", "").strip(),
                          os.getenv("WB_CONTENT_TOKEN", "").strip() or None)
        _UI_STATE["last_result"] = sync_once(_account(), client, int(time.time()) - 86400 * 14)
        _UI_STATE["last_error"] = ""
    except Exception as exc:
        # Do not leak token-bearing URLs or WB response bodies to a dashboard.
        _UI_STATE["last_error"] = type(exc).__name__
    finally:
        _UI_STATE["running"] = False


@wb_fbs_v11_bp.get("/api/wb-v11/status")
def manager_wb_status_v11():
    if not manager_access():
        return jsonify(error="Только для руководителя"), 403
    if not _allowed():
        return jsonify(configured=False, enabled=False), 200
    init_schema()
    account = _account()
    orders = query_one("SELECT COUNT(*) FROM wb_fbs_orders WHERE account_id=?", (account,))[0]
    missing = query_one("""SELECT COUNT(*) FROM wb_fbs_orders o WHERE o.account_id=?
        AND NOT EXISTS (SELECT 1 FROM wb_fbs_order_skus s WHERE
        s.account_id=o.account_id AND s.order_id=o.order_id)""", (account,))[0]
    stickers = query_one("SELECT COUNT(*) FROM wb_fbs_stickers WHERE account_id=?", (account,))[0]
    errors = query_one("""SELECT COUNT(*) FROM wb_fbs_scan_events
        WHERE account_id=? AND result<>'MATCH'""", (account,))[0]
    last = query_one("""SELECT finished_at,status FROM wb_fbs_sync_runs
        WHERE account_id=? ORDER BY started_at DESC LIMIT 1""", (account,))
    return jsonify(enabled=True, configured=bool(os.getenv("WB_API_TOKEN", "").strip()),
                   orders=orders, missing_skus=missing, stickers=stickers,
                   rejected_scans=errors, last_sync=(last[0] if last else None),
                   last_status=(last[1] if last else None),
                   **dict(_UI_STATE))


@wb_fbs_v11_bp.post("/api/wb-v11/sync")
def manager_wb_sync_v11():
    if not manager_access():
        return jsonify(error="Только для руководителя"), 403
    if not _allowed() or not os.getenv("WB_API_TOKEN", "").strip():
        return jsonify(error="WB FBS v1.1 отключён или нет API-ключа"), 503
    csrf = session.get("wb_fbs_v11_csrf") or ""
    if not csrf or not secrets.compare_digest(
        request.headers.get("X-CSRF-Token", ""), csrf
    ):
        return jsonify(error="Неверный CSRF-токен"), 403
    if _UI_STATE["running"] or _SYNC_LOCK.locked():
        return jsonify(running=True), 202
    threading.Thread(target=_manual_sync, daemon=True, name="wb-fbs-v11-manual").start()
    return jsonify(running=True), 202


MANAGER_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Wildberries FBS</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#09121e;color:#edf6ff;font-family:system-ui,Segoe UI,sans-serif;padding:30px 16px}
main{max-width:900px;margin:auto}a{color:#9bcfff;text-decoration:none}
.card{background:#142337;border:1px solid #2d445a;border-radius:13px;padding:20px;margin:18px 0}
h1{font-size:25px;margin:20px 0}.metric{display:inline-block;margin:12px 20px 12px 0}
.metric b{font-size:24px;display:block;color:#e58a34}.muted{color:#a5bacd}
button{background:#e58a34;border:0;border-radius:8px;padding:12px 18px;font-weight:750;cursor:pointer}
</style></head><body><main><a href="/manager">← Панель руководителя</a>
<h1>Интеграции · Wildberries · FBS</h1>
<div class="card"><p class="muted">Только чтение из WB API. Сканирование: товар → QR WB.</p>
<div id="stats">Загрузка статуса…</div>
<button onclick="syncNow()">Синхронизировать сейчас</button>
<p class="muted" id="message">Никакие WB-статусы и поставки не изменяются.</p></div>
<script>
const csrfToken=CSRF_VALUE;
async function refresh(){
 const r=await fetch('/api/wb-v11/status',{cache:'no-store'});const d=await r.json();
 if(!r.ok){document.getElementById('stats').textContent=d.error||'Ошибка доступа';return}
 if(!d.enabled){document.getElementById('stats').textContent='Модуль отключён. Требуется явное разрешение администратора.';return}
 document.getElementById('stats').innerHTML=
 ['Заказов','Без SKU','Стикеров','Отклонено сканов'].map((title,i)=>
 '<span class="metric"><b>'+[d.orders,d.missing_skus,d.stickers,d.rejected_scans][i]+'</b>'+title+'</span>').join('')+
 '<p class="muted">Последняя синхронизация: '+(d.last_sync||'не запускалась')+
 '; статус: '+(d.last_status||'нет')+'</p>';
 document.getElementById('message').textContent=d.running?'Синхронизация выполняется':(d.last_error?'Ошибка: '+d.last_error:'Готово');
}
async function syncNow(){
 const r=await fetch('/api/wb-v11/sync',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});
 const d=await r.json();document.getElementById('message').textContent=d.error||'Синхронизация запущена';
 refresh();
}
refresh();setInterval(refresh,10000);
</script></main></body></html>"""


@wb_fbs_v11_bp.get("/manager/wb-fbs-v11")
def manager_wb_page_v11():
    if not manager_access():
        from flask import redirect
        return redirect("/manager-login")
    token = secrets.token_urlsafe(32)
    session["wb_fbs_v11_csrf"] = token
    return Response(MANAGER_PAGE.replace("CSRF_VALUE", json.dumps(token)),
                    mimetype="text/html", headers={"Cache-Control": "no-store"})

