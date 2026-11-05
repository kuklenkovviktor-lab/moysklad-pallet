"""Conservative, read-only PDF intake for future ONIX FBS route mapping.

Stage 1 diagnoses a real seller PDF safely before any order / SKU mapping is
enabled. PDF bytes and page text are not persisted; they live in the request.
"""
import hashlib
import io
import re
from pathlib import Path
from fbs_pdf_physical_test import physical_test_enabled, prepare_test_orders

from flask import Blueprint, jsonify, redirect, request

from wms import manager_access

fbs_pdf_bp = Blueprint("fbs_pdf", __name__)
MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 10000
MAX_SAMPLE_PAGES = 30
MAX_TEXT_PER_PAGE = 3600
MAX_LINE_CHARS = 250
MAX_LINES_PER_PAGE = 50

# These are display-only hints, never authoritative data for stock or picking.
ARTICLE_HINT = re.compile(
    r"(?iu)(?:артикул|арт\.|seller\s*sku|vendor\s*code)\s*"
    r"[:№#\-]?\s*([a-zа-я0-9][a-zа-я0-9._/-]{1,85})"
)
NUMBER_HINT = re.compile(r"(?<!\d)\d{8,20}(?!\d)")


def _sample_indices(count):
    """Include the start, end and several middle points of large files."""
    if count <= MAX_SAMPLE_PAGES:
        return list(range(count))
    indexes = set(range(12))
    indexes.update(range(max(0, count - 9), count))
    middle = count // 2
    indexes.update(range(max(0, middle - 2), min(count, middle + 3)))
    for ratio in (0.25, 0.75, 0.1, 0.9):
        indexes.add(min(count - 1, int((count - 1) * ratio)))
    return sorted(indexes)[:MAX_SAMPLE_PAGES]


def inspect_pdf(content):
    """Inspect PDF text and visual structure without constructing FBS orders.

    Raises ValueError for unsupported/unsafe inputs and returns a sanitized
    diagnostic object intended for a manager-only preview.
    """
    if not isinstance(content, bytes) or not content:
        raise ValueError("PDF-файл пустой")
    if len(content) > MAX_BYTES:
        raise ValueError("PDF слишком большой. Максимум 25 МБ")
    if not content.lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0d\x20").startswith(b"%PDF-"):
        raise ValueError("Это не PDF-файл: неверный формат содержимого")

    import fitz  # PyMuPDF
    try:
        document = fitz.open(stream=content, filetype="pdf")
    except Exception as exc:
        raise ValueError("Не удалось прочитать PDF. Возможно, файл повреждён") from exc
    try:
        if document.needs_pass:
            raise ValueError("PDF защищён паролем. Загрузи файл без шифрования")
        page_count = document.page_count
        if page_count < 1:
            raise ValueError("В PDF отсутствуют страницы")
        if page_count > MAX_PAGES:
            raise ValueError("Слишком много страниц: максимум 10 000")

        sample = _sample_indices(page_count)
        pages = []
        text_count = 0
        image_only = 0
        hints = set()
        total_numeric_hints = 0

        for page_index in sample:
            page = document.load_page(page_index)
            # Extraction is intentionally bounded; text may be absent on
            # bitmap / rendered-barcode labels.
            raw = page.get_text("text", sort=True) or ""
            has_text = bool(raw.strip())
            if has_text:
                text_count += 1
            image_count = len(page.get_images(full=False))
            if not has_text and image_count:
                image_only += 1

            lines = [
                re.sub(r"\s+", " ", line).strip()[:MAX_LINE_CHARS]
                for line in raw.splitlines()
                if line.strip()
            ][:MAX_LINES_PER_PAGE]
            shown_text = "\n".join(lines)
            if len(shown_text) > MAX_TEXT_PER_PAGE:
                shown_text = shown_text[:MAX_TEXT_PER_PAGE] + "…"

            for match in ARTICLE_HINT.finditer(raw[:20000]):
                hints.add(match.group(1))
            total_numeric_hints += len(NUMBER_HINT.findall(raw[:20000]))

            pages.append({
                "number": page_index + 1,
                "text_present": has_text,
                "text_characters": len(raw),
                "images": image_count,
                "width_mm": round(page.rect.width * 25.4 / 72, 1),
                "height_mm": round(page.rect.height * 25.4 / 72, 1),
                "lines": lines,
                "excerpt": shown_text,
            })

        issues = []
        if len(sample) < page_count:
            issues.append(
                "Просмотрена выборка страниц, не весь документ. "
                "Количество заказов и товаров ещё не подсчитано."
            )
        if text_count == 0:
            issues.append(
                "В проверенных страницах нет извлекаемого текста. "
                "Возможно, этикетки состоят из изображений: понадобится отдельное распознавание."
            )
        if image_only:
            issues.append(
                "Есть страницы с изображениями без текстового слоя. "
                "Штрихкоды на них пока не распознаются."
            )
        issues.append(
            "ШК заказов, артикулы и количества ещё не подтверждены. "
            "Никакие задания и остатки не изменены."
        )
        from fbs_pdf_labels import parse_wb_labels, attach_active_locations
        wb_report = attach_active_locations(parse_wb_labels(document))
        if wb_report is not None:
            issues = [
                "Проверен весь PDF: товарные листки и стикеры WB. "
                "Это только предварительный разбор, не запись заказов в ONIX.",
                "ШК стикеров извлечены из печатного текста. "
                "Сопоставление со сканером и API WB пока не подтверждено.",
            ]
            if not wb_report["valid_structure"]:
                issues.append("Есть ошибки структуры PDF: результат нельзя использовать для распределения заданий.")
        return {
            "wb_report": wb_report,
            "sha256": hashlib.sha256(content).hexdigest(),
            "file_bytes": len(content),
            "pages": page_count,
            "sampled_pages": len(sample),
            "text_pages_in_sample": text_count,
            "image_only_in_sample": image_only,
            "sampled_numeric_tokens": total_numeric_hints,
            "article_hints": sorted(hints)[:30],
            "page_samples": pages,
            "warnings": issues,
            "mode": "diagnostic_only",
            "orders_imported": 0,
            "physical_test_enabled": physical_test_enabled(),
            "stock_changed": False,
            "tasks_created": 0,
            "file_stored": False,
        }
    finally:
        document.close()


@fbs_pdf_bp.get("/manager/fbs-pdf")
def fbs_pdf_page():
    if not manager_access():
        return redirect("/manager-login")
    from fbs_pdf_import_ui import FBS_PDF_PAGE
    return FBS_PDF_PAGE


@fbs_pdf_bp.post("/api/manager/fbs-pdf/preview")
def fbs_pdf_preview():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if request.content_length is not None and request.content_length > MAX_BYTES + 64 * 1024:
        return jsonify(error="PDF слишком большой. Максимум 25 МБ"), 413
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").lower().endswith(".pdf"):
        return jsonify(error="Выбери PDF-файл (.pdf)"), 400
    raw = upload.stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        return jsonify(error="PDF слишком большой. Максимум 25 МБ"), 413
    try:
        result = inspect_pdf(raw)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        # Do not leak library exception details or server file paths.
        return jsonify(error="Ошибка обработки PDF. Файл не импортирован"), 422
    filename = Path((upload.filename or "").replace("\\", "/")).name[:160]
    return jsonify(filename=filename, **result)


@fbs_pdf_bp.post("/api/manager/fbs-pdf/physical-test-import")
def fbs_pdf_physical_test_import():
    """Write only PDF-scanned test order registry; no stock/tasks/live orders."""
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not physical_test_enabled():
        return jsonify(error="Тестовая загрузка доступна только в изолированном окружении"), 403
    if request.content_length is not None and request.content_length > MAX_BYTES + 64 * 1024:
        return jsonify(error="PDF больше 25 МБ"), 413
    upload = request.files.get("file")
    if upload is None or not (upload.filename or "").lower().endswith(".pdf"):
        return jsonify(error="Выбери PDF-файл"), 400
    raw = upload.stream.read(MAX_BYTES + 1)
    try:
        report = inspect_pdf(raw).get("wb_report")
        orders = prepare_test_orders(report)
    except ValueError as exc:
        return jsonify(error=str(exc)), 422
    except Exception:
        return jsonify(error="Ошибка разбора PDF; тестовые заказы не сохранены"), 422

    from wms import ensure_db, db_connect
    from datetime import datetime, timezone
    try:
        ensure_db()
        with db_connect() as conn:
            cur = conn.cursor()
            # Isolated DB. Re-import replaces only earlier test orders.
            cur.execute("DELETE FROM wb_orders WHERE order_id LIKE 'pdf-physical:%'")
            for order in orders:
                cur.execute(
                    "INSERT INTO wb_orders (order_id, wb_barcode, article, skus_json, synced_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (order["order_id"], order["wb_barcode"], order["article"],
                     order["skus_json"], datetime.now(timezone.utc).isoformat()),
                )
            conn.commit()
            cur.close()
    except Exception:
        return jsonify(error="Не удалось сохранить тестовые заказы"), 409
    return jsonify(ok=True, orders_imported=len(orders), isolated=True, strict_matching=True)
