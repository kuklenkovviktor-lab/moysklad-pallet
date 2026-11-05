"""Isolated PDF->physical scanner test configuration and data validation.

This mode refuses to run alongside a production PostgreSQL database or any
MoySklad/WB credentials. It is NOT an import path for warehouse orders.
"""
import json
import os
import re
from pathlib import Path


def physical_test_enabled():
    return (
        os.getenv("GRIT_PDF_PHYSICAL_TEST", "") == "1"
        and os.getenv("WB_STRICT_MATCHING", "") == "1"
        and not os.getenv("DATABASE_URL", "").strip()
        and Path(os.getenv("WMS_DB_PATH", "")).name == "grit_pdf_physical_test.db"
        and not any(os.getenv(k, "").strip() for k in (
            "WB_API_TOKEN", "MOYSKLAD_TOKEN", "MOYSKLAD_API_TOKEN", "MS_TOKEN",
        ))
    )


def prepare_test_orders(report):
    """Validate exact unambiguous WB-to-item mapping before any test-db write."""
    if not isinstance(report, dict) or not report.get("valid_structure"):
        raise ValueError("Структура PDF содержит ошибки. Тестовый импорт запрещён.")
    products = report.get("products") or []
    output, seen = [], set()
    for product in products:
        article = str(product.get("article") or "").strip()
        barcode = str(product.get("product_barcode") or "").strip()
        if not article or not re.fullmatch(r"\d{8,20}", barcode):
            raise ValueError("Неоднозначный артикул или ШК товара")
        for sticker in product.get("stickers") or []:
            code = str(sticker.get("barcode") or "").strip()
            if not re.fullmatch(r"\d{11}", code) or code in seen or code == barcode:
                raise ValueError("Повторный, неверный или конфликтующий ШК WB")
            seen.add(code)
            output.append({
                "order_id": "pdf-physical:" + code,
                "wb_barcode": code,
                "article": article,
                "skus_json": json.dumps([barcode], ensure_ascii=False),
            })
    if not (1 <= len(output) <= 10):
        raise ValueError("Для физического теста нужен PDF с 1–10 стикерами WB")
    if len(output) != report.get("unique_sticker_barcodes"):
        raise ValueError("Не все стикеры вошли в реестр")
    return output
