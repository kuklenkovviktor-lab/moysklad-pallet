"""No-credential integration lab must remain isolated from production WMS."""
from copy import deepcopy
from pathlib import Path
import unittest

from marketplace_lab import (
    AdapterError, DOCUMENTATION, LAB_TENANT, NORMALIZERS,
    fixtures, run_scenario,
)


class MarketplaceLabTests(unittest.TestCase):
    def test_normal_three_marketplaces(self):
        result = run_scenario("normal")
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["orders"]), 4)
        self.assertEqual(result["total_units"], 8)  # WB 2 + Ozon 3 + Yandex 4
        self.assertEqual({o["platform"] for o in result["orders"]},
                         {"wildberries", "ozon", "yandex"})
        self.assertTrue(all(o["source"] == "synthetic_fixture" for o in result["orders"]))

    def test_wb_tasks_are_not_combined_by_order_uid(self):
        wb = NORMALIZERS["wildberries"](fixtures()["wildberries"], "A")
        self.assertEqual(len(wb), 2)
        self.assertNotEqual(wb[0]["key"], wb[1]["key"])
        self.assertTrue(all(o["total_units"] == 1 for o in wb))

    def test_same_sku_cannot_merge_different_sellers(self):
        result = run_scenario("isolation")
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["orders"]), 6)
        self.assertEqual(len({o["key"] for o in result["orders"]}), 6)

    def test_duplicates_are_idempotent(self):
        result = run_scenario("duplicates")
        self.assertTrue(result["passed"])
        self.assertEqual(len(result["orders"]), 4)
        self.assertTrue(all("повторно добавлено 0" in x for x in result["checks"][3:]))

    def test_invalid_units_and_unknown_scenarios(self):
        self.assertTrue(run_scenario("invalid")["passed"])
        bad = deepcopy(fixtures()["yandex"])
        bad["orders"][0]["items"][0]["count"] = 0
        with self.assertRaises(AdapterError):
            NORMALIZERS["yandex"](bad, "A")
        with self.assertRaises(AdapterError):
            run_scenario("anything")

    def test_rate_limit_is_mock_only(self):
        res = run_scenario("rate_limit")
        self.assertTrue(res["passed"])
        self.assertEqual(res["attempts"], 0)
        self.assertEqual(res["orders"], [])

    def test_no_credentials_network_or_database_usage(self):
        src = Path("marketplace_lab.py").read_text(encoding="utf-8")
        for forbidden in ("requests.", "urllib.request", "db_connect(", "query_all(",
                          "WB_API_TOKEN", "API_KEY", "sqlite3", "httpx.", "session["):
            self.assertNotIn(forbidden, src)
        self.assertEqual(len(DOCUMENTATION), 3)

    def test_manager_only_routes(self):
        integrations = Path("integrations.py").read_text(encoding="utf-8")
        app = Path("app.py").read_text(encoding="utf-8")
        for route in ("/manager/integrations/lab", "/api/manager/integrations/lab"):
            self.assertIn('"' + route + '"', app.split("manager_paths = {", 1)[1].split("}", 1)[0])
            self.assertIn('@integrations_bp.get("' + route + '")', integrations)
        self.assertIn('if not manager_access():', integrations.split("def integration_lab_api():",1)[1])
        self.assertIn("Нет API-токенов", integrations)


if __name__ == "__main__":
    unittest.main()
