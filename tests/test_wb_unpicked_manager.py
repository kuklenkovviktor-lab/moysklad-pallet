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
