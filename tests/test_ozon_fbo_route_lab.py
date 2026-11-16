"""Synthetic-only route planning regression; no warehouse or external API calls."""
import os
import unittest

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-key")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-key")
os.environ.setdefault("SECRET_KEY", "test-flask-key")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)

from flask import Flask
from ozon_fbo_route_lab import (
    SYNTHETIC_ITEMS, plan_route, route_lab_bp
)


class RoutePlanningTests(unittest.TestCase):
    def test_size_first_is_descending_and_does_not_mutate_source(self):
        original = [dict(item) for item in SYNTHETIC_ITEMS]
        result = plan_route(SYNTHETIC_ITEMS, "size")
        volumes = [i["volume_l"] for i in result["items"]]
        self.assertEqual(volumes, sorted(volumes, reverse=True))
        self.assertEqual([i["article"] for i in SYNTHETIC_ITEMS],
                         [i["article"] for i in original])
        self.assertEqual(result["size_increases"], 0)

    def test_address_first_uses_warehouse_map_natural_order(self):
        result = plan_route(SYNTHETIC_ITEMS, "address")
        self.assertEqual([i["location"] for i in result["items"]],
                         ["А-04", "А-18", "Б-08", "Ж-12", "И-03"])

    def test_hybrid_respects_size_groups_then_address(self):
        result = plan_route(SYNTHETIC_ITEMS, "hybrid")
        self.assertEqual([i["article"] for i in result["items"]],
                         ["DEMO-BOX-XL", "DEMO-BOX-M", "DEMO-JAR-5L",
                          "DEMO-SHAMPOO", "DEMO-PACK-S"])
        self.assertEqual(result["line_switches"], 3)

    def test_invalid_mode_or_dimensions_refused(self):
        with self.assertRaises(ValueError):
            plan_route(SYNTHETIC_ITEMS, "fake")
        with self.assertRaises(ValueError):
            plan_route([{"article": "INVALID", "location": "А-01",
                         "dimensions_cm": [0, 20, 10]}])
        with self.assertRaises(ValueError):
            plan_route([{"article": "INVALID", "location": "А-01",
                         "dimensions_cm": [20, -5, 10]}])

    def test_virtual_pick_sheet_has_location_qty_dimensions_and_no_mutations(self):
        from ozon_fbo_route_lab import make_virtual_picking_sheet
        located = [
            {"article": "DEMO-BIG", "location": "А-06", "dimensions_cm": [30, 25, 20], "qty": 2},
            {"article": "DEMO-SMALL", "location": "Б-01", "dimensions_cm": [10, 10, 5], "qty": 5},
        ]
        issues = [{"article": "DEMO-MISSING", "reason": "Ячейка на карте не привязана"}]
        sheet = make_virtual_picking_sheet(located, issues, "address")
        self.assertEqual(sheet["positions"], 2)
        self.assertEqual(sheet["units"], 7)
        self.assertEqual([x["location"] for x in sheet["rows"]], ["А-06", "Б-01"])
        self.assertEqual(sheet["rows"][0]["dimensions_cm"], [30, 25, 20])
        self.assertTrue(sheet["simulation_only"])
        self.assertFalse(sheet["distance_optimized"])
        self.assertEqual(sheet["excluded"], issues)
        self.assertEqual(located[0]["article"], "DEMO-BIG")

    def test_virtual_picking_sheet_rejects_invalid_route_mode(self):
        from ozon_fbo_route_lab import make_virtual_picking_sheet
        with self.assertRaises(ValueError):
            make_virtual_picking_sheet(SYNTHETIC_ITEMS, [], "fastest")

    def test_routes_are_manager_only_and_never_read_database(self):
        app = Flask(__name__)
        app.secret_key = "route-synthetic-only"
        app.register_blueprint(route_lab_bp)
        client = app.test_client()
        self.assertEqual(client.get("/api/manager/ozon-fbo/route-lab").status_code, 403)
        self.assertEqual(client.get("/manager/ozon-fbo/route-lab").status_code, 302)
        with client.session_transaction() as sess:
            sess["manager_web"] = True
        response = client.get("/api/manager/ozon-fbo/route-lab?mode=hybrid")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json["synthetic"])
        self.assertEqual(response.json["source"], "hardcoded_demo")
        self.assertEqual(len(response.json["items"]), 5)
        self.assertEqual(client.get("/api/manager/ozon-fbo/route-lab?mode=bad").status_code, 400)
        html = client.get("/manager/ozon-fbo/route-lab").get_data(as_text=True)
        self.assertIn("#09121e", html)
        self.assertIn("ИЗОЛИРОВАННАЯ ДЕМОНСТРАЦИЯ", html)
        self.assertIn("Рассчитать маршрут", html)
        self.assertIn("Виртуальный сборочный лист Ozon FBO", html)
        self.assertIn("Печать пробного листа", html)


if __name__ == "__main__":
    unittest.main()
