"""Atomic FBS transitions. PostgreSQL serializes per shift; SQLite uses a write transaction."""
import json
import os
from contextlib import contextmanager
import uuid


@contextmanager
def shift_transaction(shift_id):
    from wms import db_connect, sql, DATABASE_URL
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if not DATABASE_URL:
                cur.execute('BEGIN IMMEDIATE')
            cur.execute(sql('SELECT employee_id, ended_at FROM shifts WHERE id=?' +
                            (' FOR UPDATE' if DATABASE_URL else '')), (shift_id,))
            yield cur, cur.fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


def active_worker(cur, shift, shift_id, employee_id, picking=True):
    from wms import sql
    if not shift or shift[0] != employee_id or shift[1]:
        raise ValueError('Активная смена не принадлежит сотруднику')
    if picking:
        cur.execute(sql('SELECT activity_type FROM activities WHERE shift_id=? AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1'), (shift_id,))
        row = cur.fetchone()
        if not row or row[0] != 'picking':
            raise ValueError('Сейчас включена не сборка')


def event(cur, employee_id, shift_id, operation, kind, code=None, quantity=None, result=None, article=None, name=None):
    from wms import sql, now_iso
    cur.execute(sql('INSERT INTO operation_events (id,employee_id,shift_id,operation_type,event_type,object_code,quantity,result,article,product_name,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)'),
                (str(uuid.uuid4()), employee_id, shift_id, operation, kind, code, quantity, result, article, name, now_iso()))


def replay(request_id, shift_id, employee_id, barcode, product_barcode, cur=None):
    from wms import query_one, sql
    if not request_id:
        return False
    query = 'SELECT shift_id,employee_id,barcode,product_barcode FROM fbs_scan_requests WHERE request_id=?'
    if cur:
        cur.execute(sql(query), (request_id,))
        row = cur.fetchone()
    else:
        row = query_one(query, (request_id,))
    if row and tuple(row) != (shift_id, employee_id, barcode, product_barcode):
        raise ValueError('Номер операции уже использован для другого скана')
    return bool(row)


def commit_scan(shift_id, employee_id, barcode, product_barcode, article, name, wb_order_id, scan_type, request_id, require_pending):
    from wms import sql, now_iso
    if os.getenv("WB_FBS_V11_ENABLED", "0") == "1":
        from wb_fbs_sync_v11 import normalize_api_sticker
        barcode = normalize_api_sticker(barcode)
    with shift_transaction(shift_id) as (cur, shift):
        if replay(request_id, shift_id, employee_id, barcode, product_barcode, cur):
            return True
        active_worker(cur, shift, shift_id, employee_id)
        cur.execute(sql('SELECT product_barcode,article,product_name FROM fbs_pending_products WHERE shift_id=? AND employee_id=?'), (shift_id, employee_id))
        pending = cur.fetchone()
        if (require_pending and not pending) or (pending and pending[0] != product_barcode):
            raise ValueError('Товар уже завершён или изменился; обнови состояние сборки')
        if barcode == product_barcode:
            raise ValueError('Повторный ШК товара вместо этикетки WB')
        if pending:
            article, name = pending[1] or article, pending[2] or name
        v11 = os.getenv("WB_FBS_V11_ENABLED", "0") == "1"
        if v11:
            from wb_fbs_sync_v11 import verify_pair
            account = os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"
            verdict = verify_pair(account, product_barcode, barcode, cursor=cur)
            if verdict["result"] != "MATCH":
                raise ValueError("WB FBS: " + verdict["result"])
            wb_order_id = verdict["order_id"]
            # Re-read the sticker on the locked order inside this transaction.
            # Reject a sticker mapping that changed during the first lookup.
            cur.execute(sql("""SELECT COUNT(*) FROM wb_fbs_stickers
                WHERE account_id=? AND order_id=? AND barcode=?"""),
                (account, wb_order_id, barcode))
            if cur.fetchone()[0] != 1:
                raise ValueError("WB-стикер изменился во время проверки")
        else:
            cur.execute('SELECT 1 FROM pdf_fbs_orders LIMIT 1')
        if not v11 and cur.fetchone():
            cur.execute(sql('SELECT article,product_barcode FROM pdf_fbs_orders WHERE wb_barcode=?'), (barcode,))
            expected = cur.fetchone()
            if not pending or not expected or expected[1] != product_barcode:
                raise ValueError('Стикер или товар не соответствует загруженному PDF')
            wb_order_id = 'pdf:' + barcode
            article = expected[0]
        elif not v11:
            from wms import WB_STRICT_MATCHING
            cur.execute(sql('SELECT order_id,article,skus_json FROM wb_orders WHERE wb_barcode=? LIMIT 1'), (barcode,))
            expected = cur.fetchone()
            if expected:
                wb_order_id = str(expected[0])
                try:
                    skus = [str(x).strip() for x in json.loads(expected[2] or '[]')]
                except (TypeError, ValueError):
                    skus = []
                if WB_STRICT_MATCHING and not (product_barcode in skus or (not skus and article and expected[1] == article)):
                    raise ValueError('Не тот товар для WB-заказа')
            elif WB_STRICT_MATCHING:
                raise ValueError('WB-заказ не найден в локальном справочнике')
        cur.execute(sql('SELECT id FROM scans WHERE barcode=?'), (barcode,))
        if cur.fetchone():
            raise ValueError('Этот заказ уже был засчитан')
        scan_id = str(uuid.uuid4())
        cur.execute(sql("INSERT INTO scans (id,shift_id,employee_id,barcode,product_barcode,article,product_name,wb_order_id,scan_type,scanned_at,marketplace) VALUES (?,?,?,?,?,?,?,?,?,?,'WB')"),
                    (scan_id, shift_id, employee_id, barcode, product_barcode, article or None, name or None, wb_order_id, scan_type, now_iso()))
        if request_id:
            cur.execute(sql('INSERT INTO fbs_scan_requests (request_id,scan_id,shift_id,employee_id,barcode,product_barcode) VALUES (?,?,?,?,?,?)'),
                        (request_id, scan_id, shift_id, employee_id, barcode, product_barcode))
        cur.execute(sql('DELETE FROM fbs_pending_products WHERE shift_id=? AND employee_id=? AND product_barcode=?'), (shift_id, employee_id, product_barcode))
        if v11:
            cur.execute(sql("""UPDATE wb_fbs_orders SET local_status='complete'
                WHERE account_id=? AND order_id=? AND local_status<>'complete'"""),
                (account, wb_order_id))
            if cur.rowcount != 1:
                raise ValueError("WB-заказ уже подтверждён или изменился")
        event(cur, employee_id, shift_id, 'fbs', 'scan', barcode, 1, (article or product_barcode) + '|' + scan_type, article or None, name or None)
    return False


def close_shift(shift_id, employee_id):
    from wms import sql, now_iso
    with shift_transaction(shift_id) as (cur, shift):
        if not shift or shift[0] != employee_id:
            raise ValueError('Смена не принадлежит сотруднику')
        if shift[1]:
            return False
        ts = now_iso()
        cur.execute(sql('UPDATE activities SET ended_at=? WHERE shift_id=? AND ended_at IS NULL'), (ts, shift_id))
        cur.execute(sql('UPDATE shifts SET ended_at=? WHERE id=?'), (ts, shift_id))
        event(cur, employee_id, shift_id, 'shift', 'end', 'supervisor-qr', result='ok')
        return True
