"""Read manager-supplied Ozon FBO XLSX packing manifests, without changing inventory.

No optional spreadsheet package is needed on Render. XML data are read directly
from the Office Open XML ZIP. All barcodes stay as text.
"""
import io
import posixpath
import re
import zipfile
from collections import OrderedDict
from xml.etree import ElementTree as ET

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG = "{http://schemas.openxmlformats.org/package/2006/relationships}"

MAX_COMPRESSED = 8 * 1024 * 1024
MAX_UNCOMPRESSED = 35 * 1024 * 1024
MAX_ROWS = 15000
MAX_ITEMS = 10000
MAX_COLUMNS = 256  # Support up to 64 cargo blocks per sheet.


def _column(ref):
    col = re.match(r"^[A-Z]+", ref or "")
    if not col:
        return -1
    value = 0
    for letter in col.group():
        value = value * 26 + ord(letter) - 64
    return value - 1


def _column_label(index):
    """Convert zero-based column number to Excel letters (including AA+)."""
    result = ""
    index += 1
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def _cell_value(cell, strings):
    # Cached formula results in summary rows are ignored; formulas in real
    # cargo rows are rejected after identifying each four-column data block.
    t = cell.get("t") or ""
    if t == "inlineStr":
        inline = cell.find(MAIN + "is")
        return "".join(inline.itertext()) if inline is not None else ""
    raw = cell.findtext(MAIN + "v")
    if raw is None:
        return ""
    if t == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            raise ValueError("Повреждён общий словарь Excel")
    return raw.strip()


def _value_text(value):
    return str(value or "").strip()


def _barcode(cell, caption, source):
    value, kind = cell[:2]
    code = _value_text(value).replace(" ", "").replace("\u00a0", "")
    if not code:
        raise ValueError(f"{source}: не указан {caption}")
    if len(code) > 64 or not re.fullmatch(r"[0-9A-Za-z_-]+", code):
        raise ValueError(f"{source}: некорректный {caption} ({code[:24]})")
    if kind in ("n", "") and ("." in code or "e" in code.lower() or len(code) > 15):
        # Excel numbers longer than 15 significant digits may be rounded.
        raise ValueError(f"{source}: {caption} должен храниться текстом, иначе Excel изменит цифры")
    return code


def _load_sheet(z, xml_path, strings):
    root = ET.fromstring(z.read(xml_path))
    rows = []
    for row in root.findall(".//" + MAIN + "sheetData/" + MAIN + "row"):
        values = {}
        for cell in row.findall(MAIN + "c"):
            idx = _column(cell.get("r", ""))
            if idx >= MAX_COLUMNS:
                if cell.find(MAIN + "v") is not None or cell.find(MAIN + "f") is not None:
                    raise ValueError("Слишком много столбцов в листе Excel (максимум 256)")
                continue
            if idx >= 0:
                values[idx] = (
                    _cell_value(cell, strings),
                    cell.get("t") or "n",
                    cell.find(MAIN + "f") is not None,
                )
        rows.append((int(row.get("r") or len(rows) + 1), values))
        if len(rows) > MAX_ROWS:
            raise ValueError("Слишком много строк в одном листе Excel")
    return rows


def parse_ozon_xlsx(blob):
    if not isinstance(blob, (bytes, bytearray)) or not blob or len(blob) > MAX_COMPRESSED:
        raise ValueError("Размер XLSX должен быть от 1 байта до 8 МБ")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, ValueError):
        raise ValueError("Файл не является корректным XLSX")
    with z:
        members = z.infolist()
        if len(members) > 200 or sum(m.file_size for m in members) > MAX_UNCOMPRESSED:
            raise ValueError("Файл Excel слишком большой после распаковки")
        workbook_xml = "xl/workbook.xml"
        if workbook_xml not in z.namelist():
            raise ValueError("Не найден файл структуры XLSX")
        strings = []
        if "xl/sharedStrings.xml" in z.namelist():
            base = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for item in base.findall(MAIN + "si"):
                strings.append("".join(item.itertext()))
        wb = ET.fromstring(z.read(workbook_xml))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rel_paths = {}
        for rel in rels.findall(PKG + "Relationship"):
            target = rel.get("Target") or ""
            if rel.get("TargetMode") == "External":
                continue
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            if path.startswith("xl/worksheets/") and path.endswith(".xml"):
                rel_paths[rel.get("Id")] = path

        manifest = OrderedDict()
        warnings = []
        errors = []
        seen = {}
        barcode_articles = {}
        gm_sources = {}
        total_lines = 0
        for sheet in wb.findall(".//" + MAIN + "sheets/" + MAIN + "sheet"):
            sheet_name = (sheet.get("name") or "").strip()
            path = rel_paths.get(sheet.get(REL + "id"))
            if not path or path not in z.namelist():
                continue
            rows = _load_sheet(z, path, strings)
            if not rows:
                continue
            headers = rows[0][1]
            blocks = []
            # Each manifest block starts with its own "ШК товара" header.
            # Blank separator columns are allowed (A:D, E:H, J:M, O:R).
            # Do not infer block origins from fixed multiples of four.
            for start in range(MAX_COLUMNS - 3):
                first = _value_text(headers.get(start, ("", ""))[0]).casefold()
                if not ("шк" in first and ("товар" in first or first == "шк")):
                    continue
                captions = [_value_text(headers.get(start + i, ("", ""))[0]).casefold() for i in range(4)]
                if "шк" not in captions[0] or "кол" not in captions[2] or "шк" not in captions[3]:
                    errors.append(
                        f"Лист «{sheet_name}», блок {_column_label(start)}–{_column_label(start + 3)}: "
                        "ожидались колонки ШК товара, артикул, количество, ШК ГМ"
                    )
                    continue
                blocks.append((start, _value_text(headers.get(start + 1, ("", ""))[0]) or sheet_name))
            if not blocks:
                errors.append(f"Лист «{sheet_name}»: подходящих колонок поставки нет")
                continue

            for excel_row, fields in rows[1:]:
                for start, label in blocks:
                    data = [fields.get(start + i, ("", "")) for i in range(4)]
                    # Footers and summary tables have neither a product
                    # barcode nor a cargo barcode, even when totals are formulas.
                    if not _value_text(data[0][0]) and not _value_text(data[3][0]):
                        continue
                    total_lines += 1
                    if total_lines > MAX_ITEMS:
                        raise ValueError("В файле слишком много позиций")
                    location = f"{sheet_name}!{_column_label(start)}{excel_row}"
                    try:
                        if any(len(v) > 2 and v[2] for v in data):
                            raise ValueError("формула в товарной строке: замени её готовым значением")
                        barcode = _barcode(data[0], "ШК товара", location)
                        article = _value_text(data[1][0])
                        if not article or len(article) > 160:
                            raise ValueError("отсутствует или слишком длинный артикул")
                        gm = _barcode(data[3], "ШК грузоместа", location)
                        value = _value_text(data[2][0]).replace(",", ".")
                        try:
                            qty_number = float(value)
                        except ValueError:
                            raise ValueError("количество не является числом")
                        qty = int(qty_number)
                        if not 1 <= qty <= 1000000 or qty_number != qty:
                            raise ValueError("количество должно быть целым положительным")
                    except ValueError as exc:
                        errors.append(f"{location}: {exc}")
                        continue
                    article_for_barcode = barcode_articles.get(barcode)
                    if article_for_barcode and article_for_barcode.casefold() != article.casefold():
                        errors.append(
                            f"{location}: ШК товара {barcode} встречается с разными артикулами: "
                            f"{article_for_barcode} и {article}. Уточни исходный Excel."
                        )
                        continue
                    barcode_articles[barcode] = article
                    previous_source = gm_sources.get(gm)
                    if previous_source and previous_source[0] != sheet_name:
                        errors.append(
                            f"{location}: ШК грузоместа {gm} указан в разных "
                            "направлениях. Проверь Excel."
                        )
                        continue
                    # Repeated cargo in different blocks of the same sheet is
                    # valid. Exact repeated product lines are handled by seen.
                    gm_sources.setdefault(gm, (sheet_name, start))
                    key = (gm, barcode, article.casefold())
                    old = seen.get(key)
                    if old:
                        if old["qty"] == qty:
                            warnings.append(f"{location}: точный дубль строки {old['source']}, ГМ {gm}, {article}, {qty} шт. Исключён из плана.")
                        else:
                            errors.append(f"{location}: по ГМ {gm} и артикулу {article} разные количества ({old['qty']} и {qty}). Уточни исходный Excel.")
                        continue
                    cargo = manifest.get(gm)
                    if cargo and cargo["destination"] != sheet_name:
                        errors.append(f"{location}: ГМ {gm} указан в двух направлениях: {cargo['destination']} и {sheet_name}")
                        continue
                    if cargo is None:
                        cargo = {"barcode": gm, "destination": sheet_name, "label": label, "items": [], "qty": 0}
                        manifest[gm] = cargo
                    item = {"article": article, "barcode": barcode, "qty": qty, "source": location}
                    cargo["items"].append(item)
                    cargo["qty"] += qty
                    seen[key] = item
        if not manifest:
            errors.append("В Excel не найдено ни одной корректной товарной позиции")
        if len(manifest) > 500:
            errors.append("В файле слишком много грузомест")
        return {
            "cargo": list(manifest.values()),
            "warnings": warnings[:100],
            "errors": errors[:100],
            "source_rows": total_lines,
            "unique_rows": len(seen),
            "qty": sum(c["qty"] for c in manifest.values()),
            "places": len(manifest),
            "destinations": sorted({c["destination"] for c in manifest.values()}),
        }
