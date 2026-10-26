"""Tests for strict read-only WB sticker pages and repeated product sheets."""
import unittest
from unittest.mock import patch

from fbs_pdf_labels import parse_wb_labels, attach_active_locations


class Page:
    def __init__(self, text):
        self.text = text

    def get_text(self, kind, sort=True):
        assert kind == "text"
        return self.text


def product(qty, sku, wb="1023996565", barcode="2050291804984"):
    return (f"{qty} шт.\nБлюдца чайные набор 6 штук\nБренд: ONIX\n"
            f"Артикул WB: {wb}\nБаркод: {barcode}\nАртикул:\n{sku}")


def label(code):
    return "WB                                          " + code


def read(pages):
    return parse_wb_labels([Page(x) for x in pages])


class LabelsParsingTests(unittest.TestCase):
    def test_one_product_sheet_and_two_stickers(self):
        r = read([product(2, "1349_GRAFIT"), label("59299122174"), label("59298125228")])
        self.assertTrue(r["valid_structure"])
        self.assertEqual(r["pages_checked"], 3)
        self.assertEqual(r["product_sheet_pages"], 1)
        self.assertEqual(r["sticker_pages"], 2)
        self.assertEqual(r["total_declared_qty"], 2)
        self.assertEqual(r["unique_articles"], 1)
        self.assertEqual(r["products"][0]["product_barcode"], "2050291804984")
        self.assertEqual(r["products"][0]["stickers"][1]["barcode"], "59298125228")

    def test_duplicate_product_header_does_not_double_count_total(self):
        # Matches the real 630-page example: each header repeats global
        # demand but the intervening WB sticker pages split across blocks.
        r = read([
            product(4, "2472_GRAFIT"),
            label("59298622014"), label("59298346334"),
            product(11, "1649_MIX", "507045525", "2038719077400"),
            label("59304756305"),
            product(11, "1649_MIX", "507045525", "2038719077400"),
            *[label("5929807000"+str(i)) for i in range(10)],
            product(4, "2472_GRAFIT"),
            label("59299030816"), label("59299002869"),
        ])
        self.assertTrue(r["valid_structure"], r["errors"])
        self.assertEqual(r["unique_articles"], 2)
        self.assertEqual(r["repeat_product_sheets"], 2)
        self.assertEqual(r["sticker_pages"], 15)
        self.assertEqual(r["total_declared_qty"], 15)
        self.assertEqual(r["products"][0]["sheet_pages"], [1, 17])

    def test_repeated_header_conflicts_stop_validation(self):
        r = read([product(1,"ABC"), label("59298125228"),
                  product(2,"ABC",barcode="2050291804999"), label("59298125227")])
        self.assertFalse(r["valid_structure"])
        self.assertTrue(any(x["code"]=="conflicting_product" for x in r["errors"]))

    def test_missing_sticker_is_an_error_not_silently_imputed(self):
        r = read([product(2,"ABC"), label("59298125228")])
        self.assertFalse(r["valid_structure"])
        self.assertEqual(r["sticker_pages"],1)
        self.assertEqual(r["total_declared_qty"],2)
        self.assertTrue(any(x["code"]=="quantity_mismatch" for x in r["errors"]))

    def test_duplicate_sticker_fails_closed(self):
        r = read([product(2,"ABC"), label("59298125228"), label("59298125228")])
        self.assertFalse(r["valid_structure"])
        self.assertEqual(r["unique_sticker_barcodes"],1)
        self.assertTrue(any(x["code"]=="duplicate_sticker" for x in r["errors"]))

    def test_unrecognized_intermediate_page_is_not_guessed(self):
        r = read([product(2,"ABC"),"UNKNOWN",label("59298125228"),label("59298125227")])
        self.assertFalse(r["valid_structure"])
        self.assertTrue(any(x["code"]=="unknown_page" for x in r["errors"]))
        self.assertTrue(any(x["code"]=="orphan_sticker" for x in r["errors"]))

    def test_incomplete_product_is_rejected(self):
        r = read([product(1,"ABC").replace("Баркод: 2050291804984",""),
                  label("59298125228")])
        self.assertIsNone(r)

    def test_nonwb_document_returns_none(self):
        self.assertIsNone(read(["Hello world", "Another page"]))

    def test_location_mapping_uses_current_active_assignment_only(self):
        r = read([product(1,"1349_VERSACE"),label("59298125228")])
        with patch("wms.query_all",return_value=[
            ("1349_versace","А-05-2"),("1349_VERSACE","А-05")
        ]):
            mapped=attach_active_locations(r)
        self.assertTrue(mapped["locations_available"])
        self.assertEqual(mapped["multi_location_articles"],1)
        self.assertEqual(mapped["products"][0]["locations"],["А-05","А-05-2"])

    def test_database_failure_is_not_reported_as_missing_locations(self):
        r=read([product(1,"ABC"),label("59298125228")])
        with patch("wms.query_all",side_effect=RuntimeError("DB off")):
            mapped=attach_active_locations(r)
        self.assertFalse(mapped["locations_available"])
        self.assertIsNotNone(mapped["locations_error"])
        self.assertNotIn("without_location_articles",mapped)


if __name__=="__main__":
    unittest.main()
