"""Durable idempotency ledger for warehouse pallet receiving.

A retry with the same operation_id never invokes MoySklad twice. If the
external result is uncertain, keep the operation blocked for manual review.
Different physical pallets of the same SKU are allowed with different IDs.
"""
import json
from wms import db_connect, now_iso, query_one, sql


def init_receiving_operations():
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS receiving_confirmations (
                operation_id TEXT PRIMARY KEY,
                receipt_id TEXT NOT NULL,
                employee_id TEXT NOT NULL,
                barcode TEXT NOT NULL,
                requested_qty TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_receiving_confirmations_receipt_status
            ON receiving_confirmations(receipt_id, status)
        """)
        conn.commit()
        cur.close()


_FIELDS = (
    "operation_id", "receipt_id", "employee_id", "barcode", "requested_qty",
    "status", "response_json", "error", "created_at", "updated_at",
)


def read_operation(operation_id):
    row = query_one(sql(
        "SELECT " + ", ".join(_FIELDS) +
        " FROM receiving_confirmations WHERE operation_id = ?"
    ), (operation_id,))
    if not row:
        return None
    record = dict(zip(_FIELDS, row))
    record["response"] = json.loads(record["response_json"]) if record["response_json"] else None
    return record


def claim_operation(operation_id, receipt_id, employee_id, barcode, requested_qty):
    """Claim BEFORE touching MoySklad; only the one winner can perform a write."""
    now = now_iso()
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql("""
            INSERT INTO receiving_confirmations
              (operation_id, receipt_id, employee_id, barcode, requested_qty,
               status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)
            ON CONFLICT(operation_id) DO NOTHING
        """), (operation_id, receipt_id, employee_id, barcode, requested_qty, now, now))
        claimed = cur.rowcount == 1
        conn.commit()
        cur.close()
    record = read_operation(operation_id)
    if not record:
        raise RuntimeError("Операция приёмки не сохранилась в журнале")
    if (record["receipt_id"] != receipt_id or record["employee_id"] != employee_id
            or record["barcode"] != barcode or record["requested_qty"] != requested_qty):
        raise ValueError("Номер операции уже используется для другой палеты или документа")
    return claimed, record


def finish_operation(operation_id, response):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql("""
            UPDATE receiving_confirmations
            SET status = 'done', response_json = ?, error = NULL, updated_at = ?
            WHERE operation_id = ? AND status = 'processing'
        """), (json.dumps(response, ensure_ascii=False), now_iso(), operation_id))
        if cur.rowcount != 1:
            raise RuntimeError("Не удалось зафиксировать подтверждение в PostgreSQL")
        conn.commit()
        cur.close()


def mark_uncertain(operation_id, error):
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute(sql("""
            UPDATE receiving_confirmations SET status = 'uncertain', error = ?,
                updated_at = ? WHERE operation_id = ? AND status = 'processing'
        """), (str(error)[:400], now_iso(), operation_id))
        conn.commit()
        cur.close()


def has_unresolved_operations(receipt_id):
    row = query_one(sql("""
        SELECT COUNT(*) FROM receiving_confirmations
        WHERE receipt_id = ? AND status IN ('processing', 'uncertain')
    """), (receipt_id,))
    return bool(row and row[0])
