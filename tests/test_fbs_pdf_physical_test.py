"""Isolated physical PDF FBS test gates and matching source coverage."""
import os
import unittest
from unittest.mock import patch
from pathlib import Path
from fbs_pdf_physical_test import prepare_test_orders, physical_test_enabled

def report(code="59206990266", sku="SKU-DEMO-1", barcode="2038719091772"):
    return {"valid_structure": True, "unique_sticker_barcodes": 1,
            "products": [{"article":sku,"product_barcode":barcode,"stickers":[{"barcode":code}]}]}

class PhysicalPdfTest(unittest.TestCase):
    def test_default_off(self):
        with patch.dict(os.environ,{},clear=True):
            self.assertFalse(physical_test_enabled())

    def test_correct_sandbox_only(self):
        env={"GRIT_PDF_PHYSICAL_TEST":"1","WB_STRICT_MATCHING":"1",
             "WMS_DB_PATH":"/tmp/grit_pdf_physical_test.db"}
        with patch.dict(os.environ,env,clear=True):
            self.assertTrue(physical_test_enabled())
        for override in ({"DATABASE_URL":"postgres://real"},{"MOYSKLAD_TOKEN":"real"},
                         {"WB_API_TOKEN":"token"},{"WMS_DB_PATH":"/tmp/wms.db"},
                         {"WB_STRICT_MATCHING":"0"}):
            with self.subTest(override=override),patch.dict(os.environ,{**env,**override},clear=True):
                self.assertFalse(physical_test_enabled())

    def test_correct_order_code_and_item_barcode(self):
        o=prepare_test_orders(report())[0]
        self.assertEqual(o["wb_barcode"],"59206990266")
        self.assertEqual(o["skus_json"],'["2038719091772"]')

    def test_invalid_pdf_no_import(self):
        p=report();p["valid_structure"]=False
        with self.assertRaises(ValueError):prepare_test_orders(p)

    def test_duplicate_sticker(self):
        p=report();p["products"][0]["stickers"].append({"barcode":"59206990266"})
        p["unique_sticker_barcodes"]=2
        with self.assertRaises(ValueError):prepare_test_orders(p)

    def test_missing_product_barcode(self):
        with self.assertRaises(ValueError):prepare_test_orders(report(barcode=""))

    def test_too_many_stickers(self):
        p=report()
        p["products"][0]["stickers"]=[{"barcode":str(59000000000+i)} for i in range(11)]
        p["unique_sticker_barcodes"]=11
        with self.assertRaises(ValueError):prepare_test_orders(p)

    def test_double_scan_and_wrong_item_gate_still_in_wms(self):
        text=Path(__file__).resolve().parents[1].joinpath("wms.py").read_text(encoding="utf-8")
        self.assertIn("WB_STRICT_MATCHING and not (sku_ok",text)
        self.assertIn("if barcode == product_barcode:",text)
        self.assertIn('SELECT s.id, e.name FROM scans s JOIN employees',text)

if __name__=="__main__":
    unittest.main()
