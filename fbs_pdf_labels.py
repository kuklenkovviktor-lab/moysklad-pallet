"""Strict, read-only decoder of ONIX/WB FBS product sheets and WB sticker pages.

One product sheet may be printed more than once with the *same total* count.
Actual demand comes from distinct WB sticker pages, never summed headers.
This module does not update WB order cache, WMS tasks or stock.
"""
import re
from collections import OrderedDict

PRODUCT_QTY = re.compile(r"(?m)^\s*(\d{1,5})\s*шт\.\s*$")
PRODUCT_SKU = re.compile(r"(?m)^Артикул:\s*\n?\s*([^\n]+)")
PRODUCT_WB_ID = re.compile(r"(?m)^Артикул WB:\s*(\d{6,15})\s*$")
PRODUCT_BARCODE = re.compile(r"(?m)^Баркод:\s*(\d{8,20})\s*$")
STICKER = re.compile(r"^WB\s*(\d{11})\s*$", re.DOTALL)
MAX_ERRORS_RETURNED = 100


def _match(pattern, text):
    m = pattern.search(text)
    return m.group(1).strip() if m else None


def parse_wb_labels(document):
    """Read ALL pages, detect ambiguous structures and validate every sticker.

    Do not guess unknown pages. Never sum repeated product-sheet quantities.
    Returns None for non-WB documents with no apparent product sheets.
    """
    products = OrderedDict()
    groups = []
    issues = []
    observed = {}
    current = None
    sticker_pages = 0
    product_pages = 0
    other_pages = 0

    def error(page, code, message):
        if len(issues) < MAX_ERRORS_RETURNED:
            issues.append({"page": page, "code": code, "message": message})

    for n, page in enumerate(document, 1):
        raw = page.get_text("text", sort=True) or ""
        text = raw.strip()
        is_product = ("Артикул WB:" in text or
                      bool(re.search(r"(?m)^\s*\d+\s*шт\.\s*$", text)))
        if is_product:
            qty = _match(PRODUCT_QTY, text)
            sku = _match(PRODUCT_SKU, text)
            wb_id = _match(PRODUCT_WB_ID, text)
            barcode = _match(PRODUCT_BARCODE, text)
            if not (qty and sku and wb_id and barcode) or int(qty or 0) < 1:
                error(n, "invalid_product", "Неполные данные товарного листка (артикул, ШК, количество или WB ID)")
                current = None
                continue
            product_pages += 1
            key = sku.casefold()
            product = products.get(key)
            if product is None:
                product = {
                    "article": sku, "wb_article": wb_id,
                    "product_barcode": barcode, "declared_qty": int(qty),
                    "sheet_pages": [], "stickers": [], "locations": [],
                }
                products[key] = product
            else:
                if (product["wb_article"] != wb_id or
                    product["product_barcode"] != barcode or
                    product["declared_qty"] != int(qty)):
                    error(n, "conflicting_product",
                          "Для артикула " + sku + " различаются ШК, WB ID или напечатанное количество")
            product["sheet_pages"].append(n)
            current = product
            groups.append({"sheet_page": n, "article": sku})
            continue

        matched = STICKER.fullmatch(text)
        if matched:
            sticker_pages += 1
            label = matched.group(1)
            if current is None:
                error(n, "orphan_sticker", "Стикер WB без предшествующего товарного листка")
                continue
            if label in observed:
                error(n, "duplicate_sticker", "Повтор ШК стикера со страницы " + str(observed[label]))
            else:
                observed[label] = n
            current["stickers"].append({"page": n, "barcode": label})
            continue

        other_pages += 1
        error(n, "unknown_page", "Страница не распознана ни как товарный листок, ни как стикер WB")
        current = None

    if not product_pages:
        return None

    total_declared = 0
    for item in products.values():
        real_qty = len(item["stickers"])
        item["sticker_count"] = real_qty
        item["repeat_sheets"] = len(item["sheet_pages"]) - 1
        item["quantity_matches"] = real_qty == item["declared_qty"]
        total_declared += item["declared_qty"]
        if not item["quantity_matches"]:
            error(item["sheet_pages"][0], "quantity_mismatch",
                  f"{item['article']}: в листке {item['declared_qty']} шт., стикеров {real_qty}")

    return {
        "kind": "wb_fbs_product_and_stickers",
        "pages_checked": len(document),
        "product_sheet_pages": product_pages,
        "sticker_pages": sticker_pages,
        "unrecognized_pages": other_pages,
        "unique_articles": len(products),
        "repeat_product_sheets": sum(x["repeat_sheets"] for x in products.values()),
        "unique_sticker_barcodes": len(observed),
        "total_declared_qty": total_declared,
        "valid_structure": not issues and sticker_pages == len(observed) == total_declared,
        "errors": issues,
        "products": list(products.values()),
        "counting_rule": "По уникальным ШК стикеров; повторы товарного листка не удваивают количество.",
        "barcode_verification": "ШК извлечены из печатного текста PDF, сканирование символики и сверка с API WB пока не выполнены.",
    }


def attach_active_locations(report):
    """Join the read-only PDF result with current ONIX active cell/SKU mapping.

    Failure to access mapping is surfaced, not treated as 'no cells'.
    """
    if report is None:
        return None
    from wms import query_all

    report["locations_available"] = False
    report["locations_error"] = None
    try:
        matches = query_all(
            "SELECT a.article, l.label FROM cell_sku_assignments a "
            "JOIN replenishment_locations l ON l.id=a.location_id "
            "WHERE l.active=1"
        )
    except Exception:
        report["locations_error"] = "Не удалось прочитать привязки карты. Статусы адресов недоступны."
        return report

    by_article = {}
    for sku, label in matches:
        by_article.setdefault(str(sku or "").strip().casefold(), set()).add(str(label))
    present = multi = missing = 0
    for product in report["products"]:
        labels = sorted(by_article.get(product["article"].casefold(), []))
        product["locations"] = labels
        if len(labels) == 1:
            present += 1
        elif len(labels) > 1:
            multi += 1
        else:
            missing += 1
    report["locations_available"] = True
    report["one_location_articles"] = present
    report["multi_location_articles"] = multi
    report["without_location_articles"] = missing
    return report
