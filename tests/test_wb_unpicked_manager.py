"""Synthetic, read-only tests for the WB remainder manager screen."""
import base64
import unittest
from unittest.mock import patch

from flask import Flask
import wb_unpicked_manager as module


# Valid tiny PNG: original file mock, NOT a WB barcode or printable sample.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lZkAAAAASUVORK5CYII="
)


class UnpickedManagerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "synthetic-test-only"
        self.app.register_blueprint(module.wb_unpicked_bp)
        self.client = self.app.test_client()

    def test_real_sqlite_queue_listing_and_filters(self):
        """Run actual list SQL against an isolated synthetic SQLite file."""
        import tempfile
        from pathlib import Path
        import wms
        from wb_fbs_sync_v11 import init_schema
        with tempfile.TemporaryDirectory(prefix="pr177-synthetic-") as tmp:
            with patch.object(wms, "DB_PATH", str(Path(tmp) / "synthetic.db")):
                init_schema()
                def add(oid, account, wb, local, queued, article):
                    wms.execute("""INSERT INTO wb_fbs_orders
                      (account_id,order_id,nm_id,seller_article,wb_status,local_status,synced_at)
                      VALUES(?,?,?,?,?,?,?)""",
                      (account, oid, "1863266481", article, wb, local, "synthetic"))
                    if queued:
                        wms.execute("""INSERT INTO wb_fbs_picking_queue
                          (account_id,order_id,accepted_at) VALUES(?,?,?)""",
                          (account, oid, "synthetic"))
                add("101", "default", "confirm", "pending", True, "ART-101")
                add("102", "default", "complete", "pending", True, "ART-102")
                add("103", "default", "complete", "complete", True, "ART-103")
                add("104", "default", "cancel", "pending", True, "ART-104")
                add("105", "other", "confirm", "pending", True, "ART-105")
                add("106", "default", "complete", "pending", False, "ART-106")
                for sku in ("4600000000001", "4600000000002"):
                    wms.execute("""INSERT INTO wb_fbs_order_skus
                      (account_id,order_id,sku) VALUES(?,?,?)""",
                      ("default", "101", sku))
                total, rows = module._read_page("default", "all", "", 1)
                self.assertEqual(total, 2)
                self.assertEqual({r["order_id"] for r in rows}, {"101", "102"})
                self.assertEqual(len(next(r for r in rows if r["order_id"] == "101")["product_barcodes"]), 2)
                self.assertEqual(module._read_page("default", "confirm", "", 1)[0], 1)
                self.assertEqual(module._read_page("default", "complete", "", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "ART-101", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "4600000000002", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "1863266481", 1)[0], 2)
                self.assertEqual(module._read_page("default", "all", "nonexistent", 1)[0], 0)

    def test_manager_gate(self):
        with patch.object(module, "manager_access", return_value=False):
            self.assertEqual(self.client.get("/manager/wb-fbs/unpicked").status_code, 403)
            self.assertEqual(self.client.get("/api/manager/wb-fbs/unpicked").status_code, 403)
            self.assertEqual(self.client.post("/api/manager/wb-fbs/unpicked/print").status_code, 403)

    def test_page_and_list_have_no_mutation(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "_read_page", return_value=(1, [{
                 "order_id": "123", "seller_article": "ART", "wb_status": "confirm",
                 "product_barcodes": ["4601234567890"], "sticker_available": True
             }])):
            page = self.client.get("/manager/wb-fbs/unpicked")
            self.assertIn(b"58", page.data)
            result = self.client.get("/api/manager/wb-fbs/unpicked").json
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["product_barcodes"], ["4601234567890"])

    def test_print_requires_csrf(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True):
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                                        json={"order_ids": ["123"]})
            self.assertEqual(response.status_code, 403)

    def test_print_rejects_unadmitted_orders(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[]):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"}, json={"order_ids": ["123"]})
            self.assertEqual(response.status_code, 409)

    def test_rejects_duplicate_ids_and_over_limit(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            for ids in (["123", "123"], [str(i) for i in range(51)], ["not-a-number"]):
                response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                    headers={"X-GRIT-CSRF": "synthetic"}, json={"order_ids": ids})
                self.assertEqual(response.status_code, 400)

    def test_original_png_pdf_page_dimensions(self):
        import fitz
        class MockWB:
            def __init__(self, *args, **kwargs):
                pass
            def stickers(self, ids):
                return [{"orderId": int(oid), "file": base64.b64encode(PNG).decode()} for oid in ids]
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[("123",), ("456",)]), \
             patch.object(module, "WBClient", MockWB), \
             patch.dict(module.os.environ, {"WB_API_TOKEN": "synthetic-not-real"}):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"},
                json={"order_ids": ["123", "456"]})
            self.assertEqual(response.status_code, 200, response.data[:300])
            self.assertEqual(response.mimetype, "application/pdf")
            with fitz.open(stream=response.data, filetype="pdf") as pdf:
                self.assertEqual(len(pdf), 2)
                for page in pdf:
                    self.assertAlmostEqual(page.rect.width, 58 * 72 / 25.4, places=2)
                    self.assertAlmostEqual(page.rect.height, 40 * 72 / 25.4, places=2)
                    self.assertEqual(len(page.get_images()), 1)

    def test_partial_original_sticker_response_cannot_print(self):
        class MockWB:
            def __init__(self, *a, **kw):
                pass
            def stickers(self, ids):
                return [{"orderId": 123, "file": base64.b64encode(PNG).decode()}]
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[("123",), ("456",)]), \
             patch.object(module, "WBClient", MockWB), \
             patch.dict(module.os.environ, {"WB_API_TOKEN": "synthetic-not-real"}):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"},
                json={"order_ids": ["123", "456"]})
            self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
