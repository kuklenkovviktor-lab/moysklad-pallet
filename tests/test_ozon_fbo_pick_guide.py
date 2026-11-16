"""Synthetic-only tests for optional Ozon FBO picking hints.

Never contacts Google Sheets, real warehouse orders, scanners or the network.
"""
import unittest
from unittest.mock import patch

from ozon_fbo_pick_guide import create_picking_guide, order_picking_items


class SimplePickingGuideTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"id": "small", "article": "DEMO-S", "remaining": 2},
            {"id": "big", "article": "DEMO-L", "remaining": 1},
            {"id": "missing", "article": "DEMO-NO", "remaining": 2},
            {"id": "done", "article": "DEMO-XL", "remaining": 0},
        ]
        self.catalog = {
            "items": {
                "demo-s": {"dimensions_cm": [10, 10, 10]},
                "demo-l": {"dimensions_cm": [30, 20, 20]},
                "demo-xl": {"dimensions_cm": [50, 40, 20]},
            },
            "conflicts": {"demo-no": {"rows": [3, 9]}},
        }

    def test_size_descending_with_completed_items_last(self):
        guide = create_picking_guide(self.items, self.catalog, [
            ("DEMO-S", "А-04"), ("DEMO-L", "Б-08"),
            ("DEMO-XL", "Ж-01")
        ])
        self.assertEqual([item["id"] for item in
                          order_picking_items(self.items, guide)],
                         ["big", "small", "missing", "done"])
        self.assertEqual(guide["big"]["location"], "Б-08")
        self.assertEqual(guide["big"]["volume_cm3"], 12000)
        self.assertEqual(guide["missing"]["dimensions_state"], "conflict")
        self.assertEqual(guide["missing"]["location_state"], "missing")
        self.assertEqual(self.items[0]["id"], "small")

    def test_ambiguous_bins_not_guessed(self):
        guide = create_picking_guide(self.items, self.catalog,
                                     [("DEMO-S", "А-04"), ("DEMO-S", "Б-01")])
        self.assertEqual(guide["small"]["location_state"], "multiple")
        self.assertIsNone(guide["small"]["location"])

    def test_same_bin_repeated_is_valid(self):
        guide = create_picking_guide(self.items, self.catalog,
                                     [("DEMO-S", "А-04"), ("demo-s", "А-04")])
        self.assertEqual(guide["small"]["location"], "А-04")

    def test_worker_ui_mentions_free_order_and_locations(self):
        from ozon_fbo_picking_ui import WORKER_PICK_HTML
        self.assertIn("От большего к меньшему", WORKER_PICK_HTML)
        self.assertIn("Произвольный", WORKER_PICK_HTML)
        self.assertIn("pick-location", WORKER_PICK_HTML)
        self.assertIn("Можно сканировать в любом порядке", WORKER_PICK_HTML)

    def test_worker_hint_endpoint_read_only_and_network_mocked(self):
        from flask import Flask
        from ozon_fbo_picking import fbo_picking_bp
        app = Flask(__name__)
        app.secret_key = "synthetic-guidance-test"
        app.testing = True
        app.register_blueprint(fbo_picking_bp)
        client = app.test_client()
        url = "/api/ozon-fbo/pick/cargo/fake/guide"
        with patch("ozon_fbo_picking._worker", return_value=None):
            self.assertEqual(client.get(url).status_code, 401)
        fake = {"id": "fake", "items": self.items}
        with patch("ozon_fbo_picking._worker", return_value={"id": "tester"}), \
             patch("ozon_fbo_picking._ensure_picking_db"), \
             patch("ozon_fbo_picking._serialize", return_value=fake), \
             patch("ozon_fbo_picking.query_all", return_value=[("DEMO-L", "Б-08")]), \
             patch("grit_google_sheets.get_google_catalog",
                   return_value=(self.catalog, "2026-11-16T10:00:00Z", True)):
            response = client.get(url)
        self.assertEqual(response.status_code, 200, response.json)
        self.assertTrue(response.json["can_scan_any_order"])
        self.assertEqual(response.json["recommended_ids"][0], "big")
        self.assertEqual(response.json["guide"]["big"]["location"], "Б-08")

    def test_google_failure_does_not_block_picking_hint(self):
        from flask import Flask
        from ozon_fbo_picking import fbo_picking_bp
        from grit_google_sheets import GoogleSheetsSyncError
        app=Flask(__name__);app.secret_key="synthetic-2";app.register_blueprint(fbo_picking_bp)
        client=app.test_client()
        with patch("ozon_fbo_picking._worker", return_value={"id": "tester"}), \
             patch("ozon_fbo_picking._ensure_picking_db"), \
             patch("ozon_fbo_picking._serialize", return_value={"id": "fake", "items": self.items}), \
             patch("ozon_fbo_picking.query_all", return_value=[("DEMO-L","Б-08")]), \
             patch("grit_google_sheets.get_google_catalog", side_effect=GoogleSheetsSyncError("offline")):
            response=client.get("/api/ozon-fbo/pick/cargo/fake/guide")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json["dimensions_available"])
        self.assertTrue(response.json["can_scan_any_order"])


if __name__ == "__main__":
    unittest.main()
