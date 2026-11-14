"""Safety regression tests for Ozon FBO spreadsheet parsing.

Synthetic in-memory XLSX workbooks avoid shipping real warehouse labels.
"""
import io
import unittest
import zipfile
from xml.etree import ElementTree as ET

from ozon_fbo_parser import parse_ozon_xlsx

M = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"


def col(index):
    result = ""
    index += 1
    while index:
        index, digit = divmod(index - 1, 26)
        result = chr(65 + digit) + result
    return result


def make_xlsx(sheets):
    """Sheet rows are dictionaries of zero-based column index -> value.

    A value may be a string, integer or ("formula", expression, cached).
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        workbook = ET.Element("{%s}workbook" % M)
        workbook_sheets = ET.SubElement(workbook, "{%s}sheets" % M)
        rels = ET.Element("{%s}Relationships" % P)
        for sheet_index, (name, rows) in enumerate(sheets.items(), 1):
            ET.SubElement(workbook_sheets, "{%s}sheet" % M,
                          {"name": name, "sheetId": str(sheet_index),
                           "{%s}id" % R: "rId%d" % sheet_index})
            ET.SubElement(rels, "{%s}Relationship" % P,
                          {"Id": "rId%d" % sheet_index,
                           "Target": "worksheets/sheet%d.xml" % sheet_index})
            worksheet = ET.Element("{%s}worksheet" % M)
            data = ET.SubElement(worksheet, "{%s}sheetData" % M)
            for rowno, values in enumerate(rows, 1):
                row = ET.SubElement(data, "{%s}row" % M, {"r": str(rowno)})
                for column, value in sorted(values.items()):
                    if value is None:
                        continue
                    ref = col(column) + str(rowno)
                    cell = ET.SubElement(row, "{%s}c" % M, {"r": ref})
                    if isinstance(value, tuple):
                        _, formula, cached = value
                        ET.SubElement(cell, "{%s}f" % M).text = formula
                        if cached is not None:
                            ET.SubElement(cell, "{%s}v" % M).text = str(cached)
                    elif isinstance(value, str):
                        cell.set("t", "inlineStr")
                        inline = ET.SubElement(cell, "{%s}is" % M)
                        ET.SubElement(inline, "{%s}t" % M).text = value
                    else:
                        ET.SubElement(cell, "{%s}v" % M).text = str(value)
            z.writestr("xl/worksheets/sheet%d.xml" % sheet_index,
                       ET.tostring(worksheet, encoding="utf-8"))
        z.writestr("xl/workbook.xml", ET.tostring(workbook, encoding="utf-8"))
        z.writestr("xl/_rels/workbook.xml.rels",
                   ET.tostring(rels, encoding="utf-8"))
    return buffer.getvalue()


def header(*names):
    result = {}
    for i, name in enumerate(names):
        offset = i * 4
        result.update({offset: "ШК товара", offset+1: name,
                       offset+2: "Кол-во товаров", offset+3: "ШК ГМ"})
    return result


def cargo_row(*items):
    result = {}
    for i, (sku_barcode, article, qty, gm) in enumerate(items):
        start = 4 * i
        result.update({start: sku_barcode, start+1: article,
                       start+2: qty, start+3: gm})
    return result


class OzonFboParserTests(unittest.TestCase):
    def test_three_blocks_ignore_subtotals_without_losing_third_cargo(self):
        rows = [
            header("МСК 1", "МСК 2", "МСК 3"),
            cargo_row(("201", "SKU1", 2, "GM1"),
                      ("202", "SKU2", 3, "GM2"),
                      ("203", "SKU3", 4, "GM3")),
            {0: "204", 1: "SKU4", 2: 2, 3: "GM1",
             5: "ИТОГО", 6: ("formula", "SUM(G2:G2)", 3),
             10: ("formula", "SUM(K2:K2)", 4)},
            {2: ("formula", "SUM(C2:C3)", 4)},
        ]
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertEqual(report["errors"], [], report["errors"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["places"], 3)
        self.assertEqual(report["unique_rows"], 4)
        self.assertEqual(report["qty"], 11)
        self.assertEqual([p["label"] for p in report["cargo"]],
                         ["МСК 1", "МСК 2", "МСК 3"])

    def test_real_product_row_with_formula_is_rejected(self):
        rows = [header("МСК 1"),
                cargo_row(("201", "SKU", ("formula", "2+3", 5), "GM1"))]
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertTrue(any("формула в товарной строке" in x
                            for x in report["errors"]), report["errors"])
        self.assertEqual(report["places"], 0)

    def test_partial_product_row_rejected_instead_of_dropped(self):
        rows = [header("МСК 1"),
                cargo_row(("201", "SKU", 5, None))]
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertTrue(any("ШК грузоместа" in x for x in report["errors"]))
        self.assertEqual(report["places"], 0)

    def test_same_gm_in_two_blocks_on_same_sheet_is_one_cargo(self):
        rows = [header("МСК 1", "МСК 2"),
                cargo_row(("201", "SKU1", 2, "GM1"),
                          ("202", "SKU2", 3, "GM1"))]
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertEqual(report["errors"], [], report["errors"])
        self.assertEqual(report["places"], 1)
        self.assertEqual(report["unique_rows"], 2)
        self.assertEqual(report["qty"], 5)

    def test_separator_columns_and_exact_copy_do_not_double_count(self):
        # Synthetic counterpart of a file with A:D, E:H, blank I, J:M, blank N, O:R.
        names=["Ростов 3", "Ростов 4", "Ростов 5", "Ростов 6"]
        starts=[0,4,9,14]
        headings={}
        for start,name in zip(starts,names):
            headings.update({start:"ШК товара",start+1:name,
                             start+2:"Кол-во товаров",start+3:"ШК ГМ"})
        product1={0:"201",1:"SKU1",2:5,3:"GM2",
                  4:"201",5:"SKU1",6:5,7:"GM2",
                  9:"203",10:"SKU3",11:10,12:"GM3",
                  14:"204",15:"SKU4",16:20,17:"GM4"}
        product2={0:"202",1:"SKU2",2:3,3:"GM2",
                  4:"202",5:"SKU2",6:3,7:"GM2"}
        report=parse_ozon_xlsx(make_xlsx({"Ростов":[headings,product1,product2]}))
        self.assertEqual(report["errors"],[],report["errors"])
        self.assertEqual(report["places"],3)
        self.assertEqual(report["unique_rows"],4)
        self.assertEqual(report["qty"],38)
        self.assertEqual(len(report["warnings"]),2)

    def test_same_gm_different_quantities_across_blocks_stays_blocked(self):
        rows=[header("Ростов 3","Ростов 4"),
              cargo_row(("201","SKU1",2,"GM1"),("201","SKU1",3,"GM1"))]
        report=parse_ozon_xlsx(make_xlsx({"Ростов":rows}))
        self.assertTrue(any("разные количества" in x for x in report["errors"]))

    def test_conflict_across_destinations_rejected(self):
        sheets = {
            "мск": [header("МСК 1"), cargo_row(("201", "SKU", 2, "GM1"))],
            "спб": [header("СПБ 1"), cargo_row(("201", "SKU", 2, "GM1"))],
        }
        report = parse_ozon_xlsx(make_xlsx(sheets))
        self.assertTrue(any("в разных направлениях" in x for x in report["errors"]))

    def test_invalid_later_block_header_is_not_silently_ignored(self):
        rows = [header("МСК 1", "МСК 2"),
                cargo_row(("201", "SKU1", 2, "GM1"))]
        rows[0].update({8: "ШК товара", 9: "МСК 3",
                        10: "Кол-во товаров", 11: "Неправильный столбец"})
        rows[1].update({8: "203", 9: "SKU3", 10: 4, 11: "GM3"})
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertTrue(any("блок I–L" in x for x in report["errors"]))

    def test_column_names_and_many_blocks_after_z(self):
        rows = [header(*("место %d" % i for i in range(1, 9))),
                cargo_row(*(("%d" % (200+i), "SKU%d" % i, i+1, "GM%d" % i)
                            for i in range(8)))]
        report = parse_ozon_xlsx(make_xlsx({"мск": rows}))
        self.assertEqual(report["errors"], [], report["errors"])
        self.assertEqual(report["places"], 8)
        self.assertEqual(report["qty"], sum(range(1, 9)))
        self.assertTrue(any(i["source"] == "мск!AC2"
                            for p in report["cargo"] for i in p["items"]))


if __name__ == "__main__":
    unittest.main()
