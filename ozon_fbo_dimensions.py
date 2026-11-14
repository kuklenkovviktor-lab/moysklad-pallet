"""Read-only dimension worksheet validation for experimental GRIT route previews.

This module has no Google OAuth credentials, no warehouse mutations and no network I/O.
Export an authorized copy of "Справочник NEW +" from Google Sheets as XLSX.
"""
from decimal import Decimal, InvalidOperation
from io import BytesIO

from openpyxl import load_workbook


def _number(value, *, max_value=10000):
    try:
        number = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number > max_value:
        return None
    return float(number)


def read_dimensions_xlsx(raw):
    """Parse exact sheet columns A:E; never infer missing dimensions."""
    if not raw or len(raw) > 3 * 1024 * 1024:
        raise ValueError("Нужен XLSX до 3 МБ")
    try:
        book = load_workbook(BytesIO(raw), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("Не удалось открыть XLSX") from exc
    try:
        if "Габариты" not in book.sheetnames:
            raise ValueError('В XLSX отсутствует вкладка "Габариты"')
        sheet = book["Габариты"]
        header = next(sheet.iter_rows(min_row=1, max_row=1, max_col=5, values_only=True), ())
        expected = ("Артикул", "Длина", "Ширина", "Высота", "Вес")
        if tuple(str(c or "").strip() for c in header) != expected:
            raise ValueError("Колонки вкладки «Габариты» не соответствуют справочнику")
        entries = {}
        incomplete = []
        for rownum, values in enumerate(
                sheet.iter_rows(min_row=2, max_row=3001, max_col=5, values_only=True), 2):
            article = str(values[0] or "").strip()
            if not article:
                continue
            if len(article) > 120:
                incomplete.append({"row": rownum, "article": article[:120], "reason": "Длинный артикул"})
                continue
            dimensions = [_number(v, max_value=1000) for v in values[1:4]]
            weight = _number(values[4], max_value=10000) if values[4] is not None else None
            if any(v is None for v in dimensions):
                incomplete.append({"row": rownum, "article": article, "reason": "Нет полного набора Д×Ш×В"})
                continue
            key = article.casefold()
            value = {"article": article, "dimensions_cm": dimensions, "weight_kg": weight}
            if key in entries and entries[key]["dimensions_cm"] != dimensions:
                raise ValueError("Конфликт габаритов для артикула: " + article)
            entries[key] = value
        barcodes = {}
        if "Сводный" in book.sheetnames:
            summary = book["Сводный"]
            for cells in summary.iter_rows(min_row=3, max_row=12002, max_col=2, values_only=True):
                article = str(cells[0] or "").strip()
                barcode = str(cells[1] or "").strip()
                if not article or not barcode:
                    continue
                key = article.casefold()
                barcodes.setdefault(key, set()).add(barcode)
        return {
            "items": entries,
            "barcodes": {a: sorted(codes) for a, codes in barcodes.items()},
            "incomplete": incomplete,
            "tab_names": list(book.sheetnames),
        }
    finally:
        book.close()


def read_dimensions_values(dimension_rows, summary_rows):
    """Same catalogue schema as XLSX parser, directly from Google Sheets values."""
    header = dimension_rows[0] if dimension_rows else []
    expected = ("Артикул", "Длина", "Ширина", "Высота", "Вес")
    if tuple(str(c or "").strip() for c in (list(header) + [""] * 5)[:5]) != expected:
        raise ValueError("колонки «Габариты» не совпадают с ожидаемыми")
    entries, incomplete = {}, []
    for rownum, original in enumerate(dimension_rows[1:3001], 2):
        values = list(original) + [None] * max(0, 5 - len(original))
        article = str(values[0] or "").strip()
        if not article:
            continue
        if len(article) > 120:
            incomplete.append({"row": rownum, "article": article[:120],
                               "reason": "Длинный артикул"})
            continue
        dims = [_number(v, max_value=1000) for v in values[1:4]]
        if any(v is None for v in dims):
            incomplete.append({"row": rownum, "article": article,
                               "reason": "Нет полного набора Д×Ш×В"})
            continue
        key = article.casefold()
        weight = _number(values[4], max_value=10000) if values[4] is not None else None
        if key in entries and entries[key]["dimensions_cm"] != dims:
            raise ValueError("конфликт габаритов: " + article)
        entries[key] = {"article": article, "dimensions_cm": dims, "weight_kg": weight}
    barcodes = {}
    for original in summary_rows[:12000]:
        values = list(original) + [None, None]
        article, barcode = str(values[0] or "").strip(), str(values[1] or "").strip()
        if article and barcode:
            barcodes.setdefault(article.casefold(), set()).add(barcode)
    return {"items": entries, "incomplete": incomplete,
            "barcodes": {a: sorted(codes) for a, codes in barcodes.items()},
            "tab_names": ["Габариты", "Сводный"]}


def parse_synthetic_basket(text):
    """A manager-supplied virtual basket, never a live FBO cargo."""
    if not isinstance(text, str) or len(text) > 12000:
        raise ValueError("Слишком длинный тестовый список")
    rows = []
    for number, raw in enumerate(text.splitlines(), 1):
        if not raw.strip():
            continue
        parts = raw.rsplit(";", 1)
        if len(parts) != 2:
            raise ValueError(f"Строка {number}: укажи Артикул;Количество")
        article = parts[0].strip()
        try:
            qty = int(parts[1].strip())
        except (TypeError, ValueError):
            raise ValueError(f"Строка {number}: неверное количество")
        if not article or len(article) > 120 or not 1 <= qty <= 1000:
            raise ValueError(f"Строка {number}: неверный артикул или количество")
        rows.append((article, qty))
    if not 1 <= len(rows) <= 100:
        raise ValueError("Укажи от 1 до 100 тестовых позиций")
    if len({a.casefold() for a, q in rows}) != len(rows):
        raise ValueError("Повторяющиеся артикулы в тестовом наборе")
    return rows


def join_virtual_basket(catalog, basket, assignments):
    """Exact article joins; never manufacture an address or dimensions."""
    assigned = {}
    ambiguous = set()
    for article, label in assignments:
        key = str(article or "").strip().casefold()
        if not key:
            continue
        name = str(label or "").strip()
        if key in assigned and assigned[key] != name:
            ambiguous.add(key)
        assigned[key] = name
    located, issues = [], []
    for article, qty in basket:
        key = article.casefold()
        record = catalog["items"].get(key)
        if not record:
            issues.append({"article": article, "reason": "Габариты неизвестны"})
            continue
        if key in ambiguous:
            issues.append({"article": article, "reason": "Артикул привязан к нескольким ячейкам"})
            continue
        location = assigned.get(key)
        if not location:
            issues.append({"article": article, "reason": "Ячейка на карте не привязана"})
            continue
        located.append({
            "article": record["article"], "name": record["article"],
            "location": location, "dimensions_cm": record["dimensions_cm"],
            "weight_kg": record["weight_kg"],
            "barcodes": catalog["barcodes"].get(key, []), "qty": qty,
        })
    return located, issues
