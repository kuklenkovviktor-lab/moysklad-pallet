"""ONIX WB PDF batch intake pilot. No printing, picking or stock writes.

Stores the original validated document in DB for a future audited print workflow.
All imported stickers have unique codes within the batch.
"""
import hashlib
import io
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Blueprint, jsonify, request
from wms import DATABASE_URL, db_connect, manager_access, sql
from fbs_pdf_labels import parse_wb_labels, attach_active_locations

wb_batches_bp = Blueprint("wb_batches", __name__)
MAX_PDF_BYTES = 25 * 1024 * 1024


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _schema():
    # Binary columns deliberately differ between SQLite and PostgreSQL.
    binary_type = "BYTEA" if DATABASE_URL else "BLOB"
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""CREATE TABLE IF NOT EXISTS wb_pdf_batches (
            id TEXT PRIMARY KEY,
            sha256 TEXT NOT NULL UNIQUE,
            filename TEXT NOT NULL,
            status TEXT NOT NULL,
            imported_at TEXT NOT NULL,
            sticker_count INTEGER NOT NULL,
            sku_count INTEGER NOT NULL,
            original_pdf """ + binary_type + """ NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS wb_pdf_batch_stickers (
            batch_id TEXT NOT NULL,
            wb_code TEXT NOT NULL,
            article TEXT NOT NULL,
            page_no INTEGER NOT NULL,
            product_barcode TEXT NOT NULL,
            location_label TEXT,
            route_status TEXT NOT NULL,
            PRIMARY KEY (batch_id, wb_code),
            FOREIGN KEY (batch_id) REFERENCES wb_pdf_batches(id)
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_wb_batch_stickers_route ON wb_pdf_batch_stickers(batch_id,route_status)")
        conn.commit()
        cur.close()


def _parse(raw):
    import pymupdf as fitz
    with fitz.open(stream=raw, filetype="pdf") as doc:
        if doc.needs_pass or not 0 < doc.page_count <= 10000:
            raise ValueError("PDF пуст, защищён или превышает лимит страниц")
        report = parse_wb_labels(doc)
    if not report or not report["valid_structure"]:
        raise ValueError("Структура WB PDF не прошла полную проверку. Импорт запрещён")
    report = attach_active_locations(report)
    if not report["locations_available"]:
        raise ValueError("Адресные привязки ONIX недоступны. Импорт остановлен")
    return report


def _sticker_rows(report):
    result = []
    for product in report["products"]:
        addresses = product["locations"]
        if len(addresses) == 1:
            route_status, location = "located", addresses[0]
        elif addresses:
            route_status, location = "ambiguous", None
        else:
            route_status, location = "unassigned", None
        for sticker in product["stickers"]:
            result.append((sticker["barcode"], product["article"], sticker["page"],
                           product["product_barcode"], location, route_status))
    if len(result) != report["unique_sticker_barcodes"]:
        raise ValueError("Некорректное количество уникальных WB-стикеров")
    return result


@wb_batches_bp.post("/api/manager/wb-batches/import")
def import_batch():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if request.content_length is not None and request.content_length > MAX_PDF_BYTES + 65536:
        return jsonify(error="PDF превышает 25 МБ"), 413
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").lower().endswith(".pdf"):
        return jsonify(error="Выбери PDF Wildberries"), 400
    raw = upload.stream.read(MAX_PDF_BYTES + 1)
    if len(raw) > MAX_PDF_BYTES:
        return jsonify(error="PDF превышает 25 МБ"), 413
    if not raw.startswith(b"%PDF-"):
        return jsonify(error="Неверный формат PDF"), 400
    digest = hashlib.sha256(raw).hexdigest()
    try:
        report = _parse(raw)
        stickers = _sticker_rows(report)
        _schema()
        batch_id = str(uuid.uuid4())
        filename = Path((upload.filename or "").replace("\\", "/")).name[:160]
        with db_connect() as conn:
            cur = conn.cursor()
            # Same PDF is idempotent; one persisted batch, not two queues.
            existing = cur.execute(sql("SELECT id,sticker_count,sku_count,status FROM wb_pdf_batches WHERE sha256=?"), (digest,)).fetchone()
            if existing:
                cur.close()
                return jsonify(batch_id=existing[0], sticker_count=existing[1],
                               sku_count=existing[2], status=existing[3],
                               duplicate=True, print_jobs_created=0)
            payload = raw if DATABASE_URL else raw
            cur.execute(sql("""INSERT INTO wb_pdf_batches
                (id,sha256,filename,status,imported_at,sticker_count,sku_count,original_pdf)
                VALUES (?,?,?,?,?,?,?,?)"""),
                (batch_id,digest,filename,"imported_review_only",_utc(),
                 len(stickers),report["unique_articles"],payload))
            cur.executemany(sql("""INSERT INTO wb_pdf_batch_stickers
                (batch_id,wb_code,article,page_no,product_barcode,location_label,route_status)
                VALUES (?,?,?,?,?,?,?)"""),
                [(batch_id,*sticker) for sticker in stickers])
            conn.commit()
            cur.close()
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    except Exception:
        # Do not reveal database errors or leak PDF contents to clients.
        return jsonify(error="Импорт не завершён. Партия не подтверждена"), 500
    counts = {"located":0,"unassigned":0,"ambiguous":0}
    for row in stickers:
        counts[row[5]] += 1
    return jsonify(batch_id=batch_id, sha256=digest, status="imported_review_only",
                   sticker_count=len(stickers), sku_count=report["unique_articles"],
                   routing=counts, print_jobs_created=0, stock_changed=False,
                   picking_tasks_created=0), 201


@wb_batches_bp.get("/api/manager/wb-batches")
def list_batches():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    _schema()
    with db_connect() as conn:
        cur = conn.cursor()
        rows = cur.execute(sql("""SELECT id,filename,status,imported_at,sticker_count,sku_count
                                   FROM wb_pdf_batches ORDER BY imported_at DESC LIMIT 30""")).fetchall()
        cur.close()
    return jsonify(batches=[dict(zip(("id","filename","status","imported_at","sticker_count","sku_count"),row)) for row in rows])
