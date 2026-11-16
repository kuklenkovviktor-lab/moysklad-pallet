"""Synthetic-only WB FBS plan API and UI tests; never touches seller data."""
import os
import unittest
from unittest.mock import Mock, patch

from wb_assembly_plan import WBPlanError, count_assembly_orders, assembly_plan_status, _CACHE


class WBAssemblyPlanTests(unittest.TestCase):
    def _responses(self):
        pages = [
            {"orders":[{"id":11},{"id":12},{"id":13}],"next":100},
            {"orders":[],"next":0},
        ]
        statuses = [{"orders":[
            {"id":11,"supplierStatus":"confirm"},
            {"id":12,"supplierStatus":"new"},
            {"id":13,"supplierStatus":"complete"},
        ]}]
        def get(url, **kw):
            self.assertEqual(url, "https://marketplace-api.wildberries.ru/api/v3/orders")
            self.assertFalse(kw["allow_redirects"])
            self.assertEqual(kw["headers"]["Authorization"], "FAKE-KEY")
            x=Mock();x.raise_for_status.return_value=None;x.json.return_value=pages.pop(0);return x
        def post(url, **kw):
            self.assertEqual(url, "https://marketplace-api.wildberries.ru/api/v3/orders/status")
            self.assertEqual(kw["json"]["orders"], [11,12,13])
            x=Mock();x.raise_for_status.return_value=None;x.json.return_value=statuses.pop(0);return x
        return get,post

    def test_counts_only_confirm_and_never_changes_remote_state(self):
        get,post=self._responses()
        result=count_assembly_orders("FAKE-KEY", get=get, post=post, now=1800000000)
        self.assertEqual(result["assembly_orders"],1)
        self.assertEqual(result["checked_orders"],3)
        self.assertEqual(result["window_days"],30)

    def test_missing_status_never_generates_partial_plan(self):
        def get(*args,**kwargs):
            m=Mock();m.raise_for_status.return_value=None
            m.json.return_value={"orders":[{"id":1}],"next":0};return m
        def post(*args,**kwargs):
            m=Mock();m.raise_for_status.return_value=None
            m.json.return_value={"orders":[]};return m
        with self.assertRaises(WBPlanError):
            count_assembly_orders("FAKE",get=get,post=post)

    def test_pagination_limit_fails_closed(self):
        def get(*args,**kwargs):
            m=Mock();m.raise_for_status.return_value=None
            m.json.return_value={"orders":[{"id":1}],"next":123};return m
        with self.assertRaises(WBPlanError):
            count_assembly_orders("FAKE",get=get,post=lambda *_a,**_kw:None,max_pages=1)

    def test_live_disabled_even_when_secret_present(self):
        _CACHE.update(at=0,result=None)
        with patch.dict(os.environ,{"WB_API_TOKEN":"FAKE-KEY",
                                    "GRIT_WB_ASSEMBLY_PLAN_LIVE":"0"}), \
             patch("wb_assembly_plan.requests.get") as get:
            self.assertEqual(assembly_plan_status()["state"],"disabled")
            get.assert_not_called()

    def test_dashboard_has_manager_route_and_source_label(self):
        from pathlib import Path
        app = Path("app.py").read_text(encoding="utf-8")
        wms = Path("wms.py").read_text(encoding="utf-8")
        self.assertIn('"/api/manager/integrations/wildberries/assembly-plan"',app)
        self.assertIn("WB · На сборке",wms)
        self.assertIn("wbAssemblyCount",wms)
        self.assertIn("loadWbAssemblyPlan()",wms)


if __name__ == "__main__":
    unittest.main()
