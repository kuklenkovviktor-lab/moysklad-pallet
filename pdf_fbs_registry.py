"""PDF FBS registry: separate from WB API orders, stock and tasks.

Only explicit manager import writes here. Existing scanner records and WB API
cache are never updated by this module. Idempotent, conflict-safe ingestion.
"""
import hashlib
import re
from datetime import datetime, timezone

MAX_IMPORT_STICKERS = 20000


def prepare_pdf_orders(report):
    if not isinstance(report, dict) or not report.get("valid_structure"):
        raise ValueError("Структура PDF содержит ошибки. Импорт запрещён.")
    result, seen, by_item_barcode = [], set(), {}
    for product in report.get("products") or []:
        article = str(product.get("article") or "").strip()
        unit = str(product.get("product_barcode") or "").strip()
        wb_id = str(product.get("wb_article") or "").strip()
        if not article or not re.fullmatch(r"\d{8,20}", unit):
            raise ValueError("Неполный артикул или штрихкод товара")
        previous = by_item_barcode.setdefault(unit, article)
        if previous.casefold() != article.casefold():
            raise ValueError("Один ШК товара принадлежит нескольким артикулам")
        for sticker in product.get("stickers") or []:
            code = str(sticker.get("barcode") or "").strip()
            if not re.fullmatch(r"\d{11}", code) or code in seen or code == unit:
                raise ValueError("Повторный или недопустимый ШК WB")
            seen.add(code)
            result.append((code, article, unit, wb_id))
            if len(result) > MAX_IMPORT_STICKERS:
                raise ValueError("Слишком много стикеров для одного импорта")
    if not result or len(result) != report.get("unique_sticker_barcodes"):
        raise ValueError("Не все стикеры PDF сопоставлены с товарами")
    return result


def import_pdf_orders(report, sha256):
    from wms import db_connect, ensure_db, sql
    rows = prepare_pdf_orders(report)
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("Не удалось проверить контрольную сумму PDF")
    ensure_db()
    imported, existing = 0, 0
    at = datetime.now(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            # Full consistency preflight. Never overwrite an existing mapped sticker.
            for code, article, barcode, wb_id in rows:
                cur.execute(sql("SELECT article, product_barcode FROM pdf_fbs_orders WHERE wb_barcode = ?"), (code,))
                old = cur.fetchone()
                if old:
                    if str(old[0]).casefold() != article.casefold() or old[1] != barcode:
                        raise ValueError("Стикер " + code + " уже привязан к другому товару")
                    existing += 1
            for code, article, barcode, wb_id in rows:
                cur.execute(
                    sql("INSERT INTO pdf_fbs_orders "
                        "(wb_barcode, article, product_barcode, wb_article, source_sha256, imported_at) "
                        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (wb_barcode) DO NOTHING"),
                    (code, article, barcode, wb_id or None, sha256, at),
                )
                imported += max(0, cur.rowcount or 0)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()
    return {"orders_imported": imported, "orders_existing": existing,
            "orders_in_file": len(rows), "source_sha256": sha256}


def status():
    from wms import ensure_db, query_one
    ensure_db()
    row = query_one("SELECT COUNT(*), MAX(imported_at) FROM pdf_fbs_orders")
    return {"orders_total": int(row[0] or 0), "last_import_at": row[1]}
