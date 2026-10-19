"""Fast first-scan FBS lookup: never enumerate full product cards inline."""
import os
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-flask-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only-placeholder")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)

import app


class FastFbsBarcodeLookupTests(unittest.TestCase):
    def setUp(self):
        with app.CACHE_LOCK:
            app.PACK_CACHE.clear()
            app.PRODUCT_BARCODE_CACHE.clear()
            app.WARMUP_STATE.update(running=False, ready=False, error="")

    def test_hot_barcode_makes_zero_api_calls(self):
        item = {"id": "x", "name": "Item", "article": "SKU1", "barcodes": [{"ean13": "4601234567890"}]}
        app._index_product(item)
        with patch.object(app.requests, "get") as req:
            product, pack = app.find_fbs_product_by_barcode("4601234567890")
        req.assert_not_called()
        self.assertEqual(product["id"], "x")
        self.assertEqual(pack["quantity"], 1)

    def test_cold_barcode_uses_one_exact_filtered_request(self):
        item = {"id": "x", "name": "Item", "article": "SKU1", "barcodes": [{"ean13": "4601234567890"}]}
        reply = Mock()
        reply.json.return_value = {"rows": [item]}
        with patch.object(app.requests, "get", return_value=reply) as req:
            with patch.object(app, "_load_full_product", side_effect=AssertionError("full-card sweep is forbidden")):
                product, pack = app.find_fbs_product_by_barcode("4601234567890")
        self.assertEqual(product["id"], "x")
        self.assertEqual(pack["quantity"], 1)
        self.assertEqual(req.call_count, 1)
        self.assertEqual(req.call_args.kwargs["params"]["filter"], "barcode=4601234567890")

    def test_filtered_api_cannot_match_different_barcode(self):
        item = {"id": "x", "barcodes": [{"ean13": "DIFFERENT"}]}
        reply = Mock()
        reply.json.return_value = {"rows": [item]}
        with patch.object(app.requests, "get", return_value=reply):
            with patch.object(app, "start_pack_warmup") as warmup:
                with self.assertRaises(app.FbsCatalogIndexPending):
                    app.find_fbs_product_by_barcode("4601234567890")
        warmup.assert_called_once()

    def test_package_barcode_never_bypasses_quantity_safety(self):
        item = {"id": "x", "name": "Box", "packs": [{"barcode": "BOX144", "quantity": 144}]}
        app._index_product(item)
        with patch.object(app.requests, "get") as req:
            product, pack = app.find_fbs_product_by_barcode("BOX144")
        req.assert_not_called()
        self.assertEqual(pack["quantity"], 144)

    def test_finished_index_returns_not_found_without_full_sweep(self):
        with app.CACHE_LOCK:
            app.WARMUP_STATE["ready"] = True
        reply = Mock()
        reply.json.return_value = {"rows": []}
        with patch.object(app.requests, "get", return_value=reply):
            with patch.object(app, "_load_full_product", side_effect=AssertionError("sweep not allowed")):
                product, pack = app.find_fbs_product_by_barcode("MISSING")
        self.assertIsNone(product)
        self.assertIsNone(pack)


if __name__ == "__main__":
    unittest.main()
