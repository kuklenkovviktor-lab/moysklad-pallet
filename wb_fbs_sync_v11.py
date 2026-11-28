"""WB FBS Sync v1.1: read-only WB synchronization and local product -> sticker verification.

Feature is intentionally opt-in. No WB requests are made on barcode scans.
The caller must supply the already-scanned product barcode before the WB sticker.
"""
import json
import hashlib
import os
import random
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from contextlib import contextmanager

import requests

from wms import db_connect, sql

MARKETPLACE_API = "https://marketplace-api.wildberries.ru"
CONTENT_API = "https://content-api.wildberries.ru"
# WB delivery alone does not finish GRIT picking. Orders must have been
# observed in confirm and must not be complete locally or cancelled.
PICKABLE_STATUSES = frozenset({"confirm", "complete"})
STICKER_AVAILABLE_STATUSES = frozenset({"confirm", "complete"})
BLOCKED_WB_STATUSES = frozenset({
    "sold", "canceled", "canceled_by_client", "declined_by_client",
    "defect", "canceled_by_missed_call", "canceled_by_carrier"
})
_SYNC_LOCK = threading.Lock()


@contextmanager
def _exclusive_sync(account_id):
    """No overlapping sync in this process or across PostgreSQL app/worker processes."""
    if not _SYNC_LOCK.acquire(blocking=False):
        raise RuntimeError("WB sync already running in this process")
    lock_conn = None
    lock_cur = None
    locked = False
    try:
        from wms import DATABASE_URL
        if DATABASE_URL:
            # Session-level advisory lock, held by one dedicated pooled connection.
            # All sync paths MUST go through this guard.
            lock_id = int.from_bytes(
                hashlib.sha256(("grit-wb-fbs-v11:" + account_id).encode("utf-8")).digest()[:8],
                byteorder="big", signed=True)
            context = db_connect()
            lock_conn = context.__enter__()
            lock_cur = lock_conn.cursor()
            lock_cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
            locked = bool(lock_cur.fetchone()[0])
            if not locked:
                raise RuntimeError("WB sync already running in another process")
        yield
    finally:
        if lock_cur is not None:
            try:
                if locked:
                    lock_cur.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
                    lock_cur.fetchone()
                lock_cur.close()
            finally:
                context.__exit__(None, None, None)
        elif lock_conn is not None:
            context.__exit__(None, None, None)
        _SYNC_LOCK.release()


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
        """CREATE TABLE IF NOT EXISTS wb_fbs_picking_queue (
            account_id TEXT NOT NULL, order_id TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            PRIMARY KEY(account_id,order_id))""",
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
        """CREATE INDEX IF NOT EXISTS idx_wb_fbs_picking_queue
            ON wb_fbs_picking_queue(account_id,accepted_at)""",
        """CREATE INDEX IF NOT EXISTS idx_wb_fbs_sticker_lookup
            ON wb_fbs_stickers(account_id, barcode)""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_scan_events (
            event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, order_id TEXT,
            employee_id TEXT NOT NULL, terminal_id TEXT NOT NULL,
            expected_skus TEXT NOT NULL, scanned_barcode TEXT NOT NULL,
            scanned_sticker TEXT NOT NULL, result TEXT NOT NULL,
            reason TEXT, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS wb_fbs_sticker_repair_attempts (
            account_id TEXT NOT NULL, order_id TEXT NOT NULL,
            attempted_at TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY(account_id,order_id))""",
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
                # When a status response was requested, missing order statuses
                # must NEVER fall back to stale snapshot flags.
                status = _clean(statuses.get(oid, "unknown") if statuses is not None
                                else (item.get("wbStatus") or item.get("status") or "unknown"))
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
                # Remember a confirmed order once, independent of subsequent
                # supplierStatus=complete ("delivery"). Never enroll new
                # historical/delivery orders without confirmed evidence.
                if status == "confirm":
                    cur.execute(sql("""INSERT INTO wb_fbs_picking_queue
                        (account_id,order_id,accepted_at) VALUES(?,?,?)
                        ON CONFLICT(account_id,order_id) DO NOTHING"""),
                        (account_id, oid, now))
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
                # Serialize sticker changes with concurrent order verification
                # and scanning by locking the parent order row first.
                from wms import DATABASE_URL
                stmt = """SELECT 1 FROM wb_fbs_orders
                        WHERE account_id=? AND order_id=?"""
                if DATABASE_URL:
                    stmt += " FOR UPDATE"
                cur.execute(sql(stmt), (account_id, order_id))
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


def normalize_api_sticker(value):
    """Preserve WB's API QR; repair Russian HID and scanner AIM QR prefixes."""
    code = _clean(value)
    # Strip scanner AIM ID only when the remaining value is a valid WB QR.
    # An invalid prefixed value must remain unknown, not silently rewritten.
    has_aim = len(code) > 3 and code.startswith("]Q") and code[2] in "0123456789"
    candidate_code = code[3:] if has_aim else code
    if len(candidate_code) == 9 and candidate_code[0] in ("*", ";"):
        from wb_qr_codec import QR_DATA_RE, RU_HID_TO_EN
        candidate = "*" + candidate_code[1:]
        candidate = candidate.translate(RU_HID_TO_EN)
        if QR_DATA_RE.fullmatch(candidate):
            return candidate
    return code


def accepted_product(account_id, barcode):
    """Get the article for a scanned SKU from an unassembled accepted order.
    Verification of the exact order against its sticker happens at scan two.
    """
    from wms import query_one
    return query_one("""SELECT o.seller_article
        FROM wb_fbs_order_skus s
        JOIN wb_fbs_orders o
          ON o.account_id=s.account_id AND o.order_id=s.order_id
        JOIN wb_fbs_picking_queue q
          ON q.account_id=o.account_id AND q.order_id=o.order_id
        WHERE s.account_id=? AND s.sku=?
          AND o.wb_status IN ('confirm','complete')
          AND o.local_status<>'complete'
        LIMIT 1""", (account_id, _clean(barcode)))


def verify_pair(account_id, product_barcode, sticker_barcode, cursor=None):
    """Local DB verification; optionally read using the FBS commit transaction.

    Within a PostgreSQL transaction, lock the matched order row so a status
    update cannot slip between verification and completed-scan commit.
    """
    product = _clean(product_barcode)
    # API returns the exact 9-character QR value; match raw scanned bytes.
    sticker = normalize_api_sticker(sticker_barcode)
    if not product:
        return {"result": "PRODUCT_REQUIRED", "order_id": None, "expected_skus": []}
    if not sticker:
        return {"result": "STICKER_REQUIRED", "order_id": None, "expected_skus": []}
    from wms import DATABASE_URL
    from contextlib import nullcontext
    context = db_connect() if cursor is None else nullcontext(None)
    with context as conn:
        cur = cursor if cursor is not None else conn.cursor()
        statement = """SELECT o.order_id,o.wb_status,o.local_status,o.synced_at,
            EXISTS(SELECT 1 FROM wb_fbs_picking_queue q
               WHERE q.account_id=o.account_id AND q.order_id=o.order_id)
            FROM wb_fbs_stickers s JOIN wb_fbs_orders o
            ON o.account_id=s.account_id AND o.order_id=s.order_id
            WHERE s.account_id=? AND s.barcode=?"""
        # Locking only the order row is sufficient: sticker mapping is
        # checked immediately again inside the same transaction.
        if cursor is not None and DATABASE_URL:
            statement += " FOR UPDATE OF o"
        cur.execute(sql(statement), (account_id, sticker))
        matches = cur.fetchall()
        if len(matches) != 1:
            if cursor is None:
                cur.close()
            return {"result": "AMBIGUOUS_STICKER" if matches else "UNKNOWN_STICKER",
                    "order_id": None, "expected_skus": []}
        oid, status, local_status, synced_at, accepted = matches[0]
        cur.execute(sql("""SELECT sku FROM wb_fbs_order_skus
            WHERE account_id=? AND order_id=?"""), (account_id, oid))
        allowed = sorted(x[0] for x in cur.fetchall())
        if cursor is None:
            cur.close()
    try:
        updated_at = datetime.fromisoformat(synced_at)
        stale = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds() > int(
            os.getenv("WB_FBS_MAX_STALE_SECONDS", "180"))
    except (ValueError, TypeError, AttributeError, OverflowError):
        stale = True
    if local_status == "complete":
        result = "ALREADY_COMPLETE"
    elif stale:
        result = "STALE_CACHE"
    elif status not in PICKABLE_STATUSES or not accepted:
        result = "BLOCKED_STATUS"
    elif not allowed:
        result = "MAPPING_ERROR"
    elif product not in allowed:
        result = "MISMATCH"
    else:
        result = "MATCH"
    return {"result": result, "order_id": oid, "expected_skus": allowed,
            "wb_status": status, "admitted": bool(accepted)}


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
                seller_status = _clean(item.get("supplierStatus"))
                wb_status = _clean(item.get("wbStatus"))
                # Even if supplierStatus is still "confirm", a canceled or
                # otherwise finalized WB order is not eligible for picking.
                result[_clean(item.get("id"))] = (
                    "cancel" if wb_status in BLOCKED_WB_STATUSES else seller_status
                )
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
    """One complete read-only synchronization under a per-account distributed lock."""
    with _exclusive_sync(account_id):
        run_id = str(uuid.uuid4())
        received = updated = 0
        init_schema()
        from wms import execute
        execute("""INSERT INTO wb_fbs_sync_runs
            (run_id,account_id,started_at,sync_type,status)
            VALUES(?,?,?,?,?)""", (run_id, account_id, utcnow(), "full", "running"))
        try:
            rows = {str(x["id"]): x for x in client.orders(date_from) if x.get("id") is not None}
            for item in client.new_orders():
                if item.get("id") is not None:
                    rows[str(item["id"])] = item
            received = len(rows)
            statuses = client.statuses(list(rows)) if rows else {}
            updated = ingest_orders(account_id, rows.values(), statuses)
            wanted = [int(oid) for oid, status in statuses.items()
                      if status in STICKER_AVAILABLE_STATUSES and oid in rows]
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


# Manager-facing control is explicitly disabled until the opt-in flag is set.
from flask import Blueprint, Response, jsonify, request, session
from wms import manager_access, query_one

wb_fbs_v11_bp = Blueprint("wb_fbs_v11", __name__)
_UI_STATE = {"running": False, "last_error": "", "last_result": None}


def _allowed():
    return os.getenv("WB_FBS_V11_ENABLED", "0") == "1"


def _account():
    return os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"


def _configured_scan_source():
    """Server-selected source, not a request or an employee toggle."""
    return os.getenv("WB_FBS_SOURCE_MODE", "api").strip().lower()


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
    missing_stickers = query_one("""SELECT COUNT(*)
        FROM wb_fbs_orders o
        JOIN wb_fbs_picking_queue q
          ON q.account_id=o.account_id AND q.order_id=o.order_id
        LEFT JOIN wb_fbs_stickers s
          ON s.account_id=o.account_id AND s.order_id=o.order_id
        WHERE o.account_id=? AND o.local_status='pending'
          AND o.wb_status IN ('confirm','complete')
          AND (s.order_id IS NULL OR s.barcode IS NULL OR s.barcode=''
               OR LENGTH(s.barcode)<>9 OR SUBSTR(s.barcode,1,1)<>'*')""",
          (account,))[0]
    errors = query_one("""SELECT COUNT(*) FROM wb_fbs_scan_events
        WHERE account_id=? AND result<>'MATCH'""", (account,))[0]
    last = query_one("""SELECT finished_at,status FROM wb_fbs_sync_runs
        WHERE account_id=? ORDER BY started_at DESC LIMIT 1""", (account,))
    last_repair = query_one("""SELECT MAX(attempted_at)
        FROM wb_fbs_sticker_repair_attempts WHERE account_id=?""", (account,))
    # The API cache and imported PDFs are independent order registries.
    # Never silently switch source: pdf_picking_active() historically applies
    # to all active shifts as soon as any PDF is imported.
    pdf_count = query_one("SELECT COUNT(*) FROM pdf_fbs_orders")[0]
    return jsonify(enabled=True, configured=bool(os.getenv("WB_API_TOKEN", "").strip()),
                   orders=orders, missing_skus=missing, stickers=stickers,
                   missing_stickers=missing_stickers,
                   scan_source=_configured_scan_source(), pdf_orders_present=bool(pdf_count),
                   pdf_verification_separate=True,
                   last_sticker_repair=(last_repair[0] if last_repair else None),
                   rejected_scans=errors, last_sync=(last[0] if last else None),
                   last_status=(last[1] if last else None),
                   **dict(_UI_STATE))


@wb_fbs_v11_bp.post("/api/wb-v11/diagnose-sticker")
def manager_wb_diagnose_sticker_v11():
    """Manager-only, read-only reason for an unknown WB QR.

    Do not allow worker scans to trigger network access or mutate orders.
    Output contains only classification flags, not WB order/customer details.
    """
    if not manager_access():
        return jsonify(error="Только для руководителя"), 403
    csrf = session.get("wb_fbs_v11_csrf") or ""
    if not csrf or not secrets.compare_digest(
            request.headers.get("X-CSRF-Token",""), csrf):
        return jsonify(error="Неверный CSRF-токен"), 403
    if not _allowed():
        return jsonify(error="Режим WB FBS v1.1 выключен"), 503
    data = request.get_json(silent=True) or {}
    if not isinstance(data,dict):
        return jsonify(error="Неверный запрос"), 400
    entered = data.get("sticker")
    if not isinstance(entered,str) or not 1 <= len(entered) <= 128:
        return jsonify(error="Нужен скан стикера WB"), 400
    normalized = normalize_api_sticker(entered)
    from wb_qr_codec import canonical_wb_sticker, QR_DATA_RE
    from wms import query_one
    # Schema is initialized at WMS startup. Never execute DDL from a
    # read-only manager diagnosis request.
    account = _account()
    own = query_one("""SELECT COUNT(*) FROM wb_fbs_stickers
        WHERE account_id=? AND barcode=?""",(account,normalized))[0]
    # Never query other WB accounts from a tenant-scoped diagnosis.
    # An unknown QR may belong to another account, but disclosing which
    # account has it would be a cross-tenant information leak.
    canonical=canonical_wb_sticker(entered)
    numeric = canonical if canonical.isdigit() and len(canonical)==11 else ""
    pdf_match = bool(numeric and query_one(
        "SELECT 1 FROM pdf_fbs_orders WHERE wb_barcode=? LIMIT 1",(numeric,)))
    qr_valid = QR_DATA_RE.fullmatch(normalized) is not None
    if _configured_scan_source() == "pdf":
        if pdf_match:
            reason="IN_SELECTED_PDF"
        elif not qr_valid and not numeric:
            reason="NOT_A_RECOGNIZED_WB_CODE"
        else:
            reason="NOT_IN_PDF_REGISTRY"
    elif own == 1:
        reason="IN_SELECTED_ACCOUNT"
    elif own > 1:
        reason="DUPLICATE_CURRENT_ACCOUNT"
    elif pdf_match:
        reason="PDF_REGISTRY_ONLY"
    elif not qr_valid:
        reason="NOT_A_NINE_CHARACTER_WB_QR"
    else:
        reason="NOT_IN_WB_API_CACHE"
    response=jsonify(source=_configured_scan_source(),account_has_match=bool(own),
        imported_pdf_has_match=pdf_match,qr_format_valid=qr_valid,
        reason=reason,needs_manager_review=reason not in
        ("IN_SELECTED_ACCOUNT","IN_SELECTED_PDF"))
    response.headers["Cache-Control"]="no-store"
    return response


@wb_fbs_v11_bp.post("/api/wb-v11/sync")
def manager_wb_sync_v11():
    if not manager_access():
        return jsonify(error="Только для руководителя"), 403
    if not _allowed() or not os.getenv("WB_API_TOKEN", "").strip():
        return jsonify(error="WB FBS v1.1 отключён или нет API-ключа"), 503
    if _configured_scan_source() != "api":
        return jsonify(error="Выбран PDF-источник. Фоновая синхронизация WB не активирует этот режим."), 409
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
<div style="margin-top:18px">
  <strong>Почему стикер не найден?</strong>
  <p class="muted">Проверка только по локальным данным, без изменения заказов WB.</p>
  <input id="stickerLookup" autocomplete="off" maxlength="128"
   placeholder="Сканируй QR WB" style="padding:12px;background:#09121e;
   color:#edf6ff;border:1px solid #425875;border-radius:8px;max-width:100%;width:300px">
  <button type="button" onclick="diagnoseSticker()">Проверить стикер</button>
  <p class="muted" id="stickerDiagnostic" role="status"></p>
</div>
<p class="muted" id="message">Никакие WB-статусы и поставки не изменяются.</p></div>
<script>
const csrfToken=CSRF_VALUE;
async function refresh(){
 const r=await fetch('/api/wb-v11/status',{cache:'no-store'});const d=await r.json();
 if(!r.ok){document.getElementById('stats').textContent=d.error||'Ошибка доступа';return}
 if(!d.enabled){document.getElementById('stats').textContent='Модуль отключён. Требуется явное разрешение администратора.';return}
 document.getElementById('stats').innerHTML=
 ['Заказов','Без SKU','Стикеров','Без стикера','Отклонено сканов'].map((title,i)=>
 '<span class="metric"><b>'+[d.orders,d.missing_skus,d.stickers,d.missing_stickers,d.rejected_scans][i]+'</b>'+title+'</span>').join('')+
 '<p class="muted">Последняя синхронизация: '+(d.last_sync||'не запускалась')+
 '; статус: '+(d.last_status||'нет')+
 '; последняя попытка восстановления QR: '+(d.last_sticker_repair||'ещё не выполнялась')+
 (d.pdf_orders_present&&d.scan_source==='api'?'<br><strong>Внимание:</strong> загруженные PDF не используются при проверке WB API v1.1.':'')+
 '<br>Источник FBS: '+(d.scan_source==='pdf'?'PDF':d.scan_source==='api'?'WB API':'НЕВЕРНЫЙ РЕЖИМ')+
 '</p>';
 document.getElementById('message').textContent=d.running?'Синхронизация выполняется':(d.last_error?'Ошибка: '+d.last_error:'Готово');
}
async function syncNow(){
 const r=await fetch('/api/wb-v11/sync',{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},body:'{}'});
 const d=await r.json();document.getElementById('message').textContent=d.error||'Синхронизация запущена';
 refresh();
}
async function diagnoseSticker(){
 const field=document.getElementById('stickerLookup');
 const out=document.getElementById('stickerDiagnostic');
 out.textContent='Проверка локального кэша…';
 try{
  const r=await fetch('/api/wb-v11/diagnose-sticker',{
   method:'POST',credentials:'same-origin',cache:'no-store',
   headers:{'Content-Type':'application/json','X-CSRF-Token':csrfToken},
   body:JSON.stringify({sticker:field.value.trim()})});
  const d=await r.json();
  if(!r.ok){out.textContent=d.error||'Ошибка доступа';return}
  const names={
   IN_SELECTED_ACCOUNT:'QR есть в текущем WB API-кэше. Проверь статус заказа и ШК товара.',
   IN_SELECTED_PDF:'Стикер найден в выбранном PDF-реестре. Проверь соответствие товара.',
   NOT_IN_PDF_REGISTRY:'Стикер отсутствует в выбранном PDF-реестре. Заказ не засчитан.',
   NOT_A_RECOGNIZED_WB_CODE:'Не распознан QR или цифровой код WB.',
   DUPLICATE_CURRENT_ACCOUNT:'QR связан с несколькими заказами. Заблокировано, нужна проверка.',
   PDF_REGISTRY_ONLY:'QR найден только в загруженном PDF. Режим WB API его не использует.',
   NOT_A_NINE_CHARACTER_WB_QR:'Отсканирован не девятисимвольный QR WB. Проверь тип кода и сканер.',
   NOT_IN_WB_API_CACHE:'QR отсутствует в выбранном WB API-кэше. Проверь синхронизацию и правильность аккаунта.'};
  out.textContent=names[d.reason]||'Неизвестная причина';
 }catch(e){out.textContent='Ошибка связи с GRIT'}
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


def _sync_new_once_unlocked(account_id, client):
    """Fast 30-second discovery; only newly eligible sticker IDs are fetched."""
    init_schema()
    orders = client.new_orders()
    if not orders:
        return {"received": 0, "updated": 0}
    ids = [_clean(x.get("id")) for x in orders if x.get("id") is not None]
    statuses = client.statuses(ids)
    updated = ingest_orders(account_id, orders, statuses)
    from wms import query_all
    existing = {row[0] for row in query_all(
        "SELECT order_id FROM wb_fbs_stickers WHERE account_id=?", (account_id,))}
    eligible = [int(oid) for oid in ids
                if statuses.get(oid) in STICKER_AVAILABLE_STATUSES and oid not in existing]
    if eligible:
        ingest_stickers(account_id, client.stickers(eligible))
    return {"received": len(orders), "updated": updated}


def _sync_missing_stickers_once_unlocked(account_id, client):
    """Repair missing WB stickers for already-known active picking orders.

    An order may be cached before its sticker becomes available. The old
    status worker fetched stickers only for orders without a sticker at
    that exact moment; failure or eventual WB availability left a durable
    hole until a full/manual sync. This bounded repair runs in the existing
    read-only WB worker under its per-account lock, never in /fbs/scan.

    Do NOT fabricate a sticker, infer its QR from printed digits, or accept
    an order when WB API does not return a verified barcode.
    """
    from wms import query_all, execute
    init_schema()
    retry_before = datetime.fromtimestamp(
        time.time() - 300, timezone.utc).isoformat()
    missing = query_all("""SELECT o.order_id
        FROM wb_fbs_orders o
        JOIN wb_fbs_picking_queue q
          ON q.account_id=o.account_id AND q.order_id=o.order_id
        LEFT JOIN wb_fbs_stickers s
          ON s.account_id=o.account_id AND s.order_id=o.order_id
        LEFT JOIN wb_fbs_sticker_repair_attempts r
          ON r.account_id=o.account_id AND r.order_id=o.order_id
        WHERE o.account_id=?
          AND o.local_status='pending'
          AND o.wb_status IN ('confirm','complete')
          AND (s.order_id IS NULL OR s.barcode IS NULL OR s.barcode=''
               OR LENGTH(s.barcode)<>9 OR SUBSTR(s.barcode,1,1)<>'*')
          AND (r.attempted_at IS NULL OR r.attempted_at<=?)
        ORDER BY CASE WHEN r.attempted_at IS NULL THEN 0 ELSE 1 END,
                 r.attempted_at ASC,o.synced_at DESC,o.order_id DESC
        LIMIT 100""", (account_id,retry_before))
    if not missing:
        return {"missing_checked": 0, "stickers_received": 0}
    # A malformed order ID must NOT be passed to the WB API as a different
    # integer or quietly reinterpreted as another valid order.
    ids = [int(row[0]) for row in missing
           if str(row[0]).isdigit() and str(int(row[0])) == str(row[0])]
    if not ids:
        return {"missing_checked": len(missing), "stickers_received": 0}
    # Persist retry checkpoints BEFORE contacting WB. If WB has not yet
    # published the sticker, the same order will not be hammered every
    # 30 seconds; old missing orders also get a turn in subsequent batches.
    now = utcnow()
    for order_id in ids:
        execute("""INSERT INTO wb_fbs_sticker_repair_attempts
            (account_id,order_id,attempted_at,attempt_count)
            VALUES(?,?,?,1)
            ON CONFLICT(account_id,order_id) DO UPDATE SET
            attempted_at=excluded.attempted_at,
            attempt_count=wb_fbs_sticker_repair_attempts.attempt_count+1""",
            (account_id,str(order_id),now))
    response = client.stickers(ids)
    allowed = {str(order_id) for order_id in ids}
    from wb_qr_codec import QR_DATA_RE, canonical_wb_sticker
    verified = []
    for item in response or []:
        if not isinstance(item,dict) or str(item.get('orderId') or '') not in allowed:
            continue
        raw = normalize_api_sticker(item.get('barcode'))
        # Only authoritative 9-symbol WB API QRs enter this repair path.
        # Never treat the printed numeric Code128 or malformed payload
        # as a replacement for an API-sourced sticker.
        if not QR_DATA_RE.fullmatch(raw):
            continue
        numeric = canonical_wb_sticker(raw)
        if not (isinstance(numeric,str) and numeric.isdigit()
                and len(numeric)==11):
            continue
        verified.append(dict(item,barcode=raw))
    count = ingest_stickers(account_id, verified) if verified else 0
    return {"missing_checked": len(ids), "stickers_received": count}


def _sync_status_once_unlocked(account_id, client):
    """Refresh active cached orders without touching WB statuses remotely."""
    from wms import execute, query_all
    init_schema()
    # Refresh NEW/CONFIRM orders, admitted unassembled deliveries even after
    # the recent window, plus other recently created orders. Do not sync the
    # entire 14-day history every 30 seconds.
    recent_cutoff = datetime.fromtimestamp(
        time.time() - 2 * 86400, timezone.utc).isoformat()
    ids = [r[0] for r in query_all("""SELECT order_id FROM wb_fbs_orders
        WHERE account_id=? AND local_status<>'complete'
          AND (wb_status IN ('new','confirm')
               OR (wb_status='complete' AND EXISTS(
                    SELECT 1 FROM wb_fbs_picking_queue q
                    WHERE q.account_id=wb_fbs_orders.account_id
                      AND q.order_id=wb_fbs_orders.order_id))
               OR created_at>=?)""",
        (account_id, recent_cutoff))]
    statuses = client.statuses(ids) if ids else {}
    # Missing order entries in the WB response cannot silently retain an old
    # pickable status. Fail closed until a complete status response is obtained.
    for oid in ids:
        statuses.setdefault(str(oid), "unknown")
    # Persist authoritative WB status changes before fetching stickers. Even
    # when the sticker endpoint fails, cancellations must already be enforced.
    for oid, status in statuses.items():
        if status not in PICKABLE_STATUSES and status not in {
            "new", "cancel", "canceled", "cancelled", "decline"}:
            status = "unknown"
        execute("""UPDATE wb_fbs_orders SET wb_status=?,synced_at=?
            WHERE account_id=? AND order_id=?""",
            (status, utcnow(), account_id, oid))
        if status == "confirm":
            execute("""INSERT INTO wb_fbs_picking_queue
                (account_id,order_id,accepted_at)
                SELECT account_id,order_id,? FROM wb_fbs_orders
                WHERE account_id=? AND order_id=?
                ON CONFLICT(account_id,order_id) DO NOTHING""",
                (utcnow(), account_id, oid))
    existing = {row[0] for row in query_all(
        "SELECT order_id FROM wb_fbs_stickers WHERE account_id=?", (account_id,))}
    eligible = [int(oid) for oid, status in statuses.items()
                if status in STICKER_AVAILABLE_STATUSES and oid not in existing]
    if eligible:
        ingest_stickers(account_id, client.stickers(eligible))
    return len(statuses)


def _sync_catalog_once_unlocked(account_id, client):
    """Six-hour read-only SKU catalog refresh."""
    init_schema()
    return ingest_catalog(account_id, client.cards())


def sync_new_once(account_id, client):
    with _exclusive_sync(account_id):
        return _sync_new_once_unlocked(account_id, client)


def sync_status_once(account_id, client):
    with _exclusive_sync(account_id):
        return _sync_status_once_unlocked(account_id, client)


def sync_missing_stickers_once(account_id, client):
    with _exclusive_sync(account_id):
        return _sync_missing_stickers_once_unlocked(account_id, client)


def sync_catalog_once(account_id, client):
    with _exclusive_sync(account_id):
        return _sync_catalog_once_unlocked(account_id, client)


def worker_loop(account_id, client, stop=None):
    """Run in exactly ONE dedicated process per WB account, never in Gunicorn."""
    if os.getenv("WB_FBS_V11_ENABLED", "0") != "1":
        raise RuntimeError("Set WB_FBS_V11_ENABLED=1 only in an isolated worker")
    if _configured_scan_source() != "api":
        raise RuntimeError("WB worker disabled: selected FBS scan source is not WB API")
    intervals = {"new": 30, "status": 30, "stickers": 300,
                 "catalog": 21600, "full": 86400}
    last = {key: -float("inf") for key in intervals}
    while stop is None or not stop.is_set():
        now = time.monotonic()
        for name, interval in intervals.items():
            if now - last[name] < interval:
                continue
            last[name] = now
            try:
                if name == "new":
                    sync_new_once(account_id, client)
                elif name == "status":
                    sync_status_once(account_id, client)
                elif name == "stickers":
                    _sync_missing_stickers_once_unlocked(account_id, client)
                elif name == "catalog" and client.content_token:
                    sync_catalog_once(account_id, client)
                elif name == "full":
                    sync_once(account_id, client, int(time.time()) - 86400 * 14)
            except Exception as exc:
                # Avoid leaking WB token or personal data in the log.
                print(f"WB FBS v1.1 {name} failed: {type(exc).__name__}", flush=True)
        if stop is not None and stop.wait(1):
            break
        if stop is None:
            time.sleep(1)


if __name__ == "__main__":
    if os.getenv("WB_FBS_V11_ENABLED", "0") != "1":
        raise SystemExit("WB FBS v1.1 worker disabled")
    worker_loop(os.getenv("WB_FBS_V11_ACCOUNT_ID", "default"),
                WBClient(os.getenv("WB_API_TOKEN", "").strip(),
                         os.getenv("WB_CONTENT_TOKEN", "").strip() or None))

