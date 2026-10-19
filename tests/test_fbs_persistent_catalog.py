"""Regression tests for persistent FBS product barcode snapshots."""
import os
import tempfile
import unittest
from unittest.mock import patch

_temp = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-flask-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only-placeholder")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_temp.name, "catalog-test.db")

import app
from fbs_catalog import (
    find_persisted_fbs_barcode,
    init_fbs_catalog_db,
    save_fbs_catalog_snapshot,
    fbs_catalog_status,
)


class PersistentCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_fbs_catalog_db()

    def setUp(self):
        with app.CACHE_LOCK:
            app.PRODUCT_BARCODE_CACHE.clear()
            app.PACK_CACHE.clear()
            app.WARMUP_STATE.update(running=False, ready=False, error="")

    def test_restarted_worker_finds_product_without_moysklad(self):
        item = {"id": "p1", "name": "Crystal", "article": "1240_CRYSTAL_NONE"}
        save_fbs_catalog_snapshot({"4601234567890": item}, {})
        with patch.object(app.requests, "get", side_effect=AssertionError("No MoySklad lookup")):
            product, pack = app.find_fbs_product_by_barcode("4601234567890")
        self.assertEqual(product["article"], "1240_CRYSTAL_NONE")
        self.assertEqual(pack["quantity"], 1)
        self.assertEqual(fbs_catalog_status()["persisted_barcodes"], 1)

    def test_pallet_barcode_keeps_quantity_restriction(self):
        item = {"id": "p2", "name": "A pallet", "article": "PALLET"}
        save_fbs_catalog_snapshot(
            {"12345": item},
            {"PALLET144": {"product": item, "pack": {"quantity": 144}}},
        )
        with patch.object(app.requests, "get", side_effect=AssertionError("No MoySklad lookup")):
            product, pack = app.find_fbs_product_by_barcode("PALLET144")
        self.assertEqual(pack["quantity"], 144)
        self.assertEqual(pack["name"], "Упаковка")

    def test_failed_empty_refresh_keeps_old_snapshot(self):
        item = {"id": "p3", "name": "Old", "article": "OLD"}
        save_fbs_catalog_snapshot({"SAVED": item}, {})
        with self.assertRaises(ValueError):
            save_fbs_catalog_snapshot({}, {})
        found, pack = find_persisted_fbs_barcode("SAVED")
        self.assertEqual(found["name"], "Old")
        self.assertEqual(pack["quantity"], 1)

    def test_refresh_replaces_outdated_codes_atomically(self):
        old = {"id": "x", "name": "Old"}
        new = {"id": "y", "name": "New"}
        save_fbs_catalog_snapshot({"OLD_CODE": old}, {})
        save_fbs_catalog_snapshot({"NEW_CODE": new}, {})
        self.assertEqual(find_persisted_fbs_barcode("OLD_CODE"), (None, None))
        self.assertEqual(find_persisted_fbs_barcode("NEW_CODE")[0]["name"], "New")


if __name__ == "__main__":
    unittest.main()
