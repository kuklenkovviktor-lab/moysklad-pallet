"""Synthetic regression: live dashboard counts the admitted GRIT WB queue only."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge-only")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal-only")
os.environ["DATABASE_URL"] = ""
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
import wms
from flask import Flask


class WBPickingQueueDashboardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = self.tmp.name + "/queue.sqlite"
        self.db_patch = patch.object(wms, "DB_PATH", self.db)
        self.db_patch.start()
        self.app = Flask(__name__)
        self.ctx = self.app.app_context()
        self.ctx.push()
        self.access = patch.object(wms, "manager_access", return_value=True)
        self.access.start()
        self.ensure = patch.object(wms, "ensure_db", return_value=None)
        self.ensure.start()
        self.enabled = patch.dict(os.environ, {"WB_FBS_V11_ENABLED": "1", "WB_FBS_V11_ACCOUNT_ID": "default"})
        self.enabled.start()
        with sqlite3.connect(self.db) as conn:
            conn.executescript("""
                CREATE TABLE wb_fbs_orders (
                    account_id TEXT, order_id TEXT, wb_status TEXT,
                    local_status TEXT, PRIMARY KEY(account_id,order_id));
                CREATE TABLE wb_fbs_picking_queue (
                    account_id TEXT, order_id TEXT, accepted_at TEXT,
                    PRIMARY KEY(account_id,order_id));
            """)

    def tearDown(self):
        self.enabled.stop()
        self.ensure.stop()
        self.access.stop()
        self.ctx.pop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def seed(self, account, oid, wb_status, local_status, admitted):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO wb_fbs_orders VALUES(?,?,?,?)",
                         (account,oid,wb_status,local_status))
            if admitted:
                conn.execute("INSERT INTO wb_fbs_picking_queue VALUES(?,?,?)",
                             (account,oid,"2026-11-23T00:00:00+00:00"))

    def test_only_admitted_pending_confirm_and_delivery(self):
        for args in [
            ("default","1","confirm","pending",True),
            ("default","2","confirm","pending",True),
            ("default","3","complete","pending",True),
            ("default","4","complete","pending",False),
            ("default","5","confirm","complete",True),
            ("default","6","cancel","pending",True),
            ("other","7","confirm","pending",True),
            ("default","8","new","pending",True),
        ]:
            self.seed(*args)
        response = wms.manager_wb_fbs_picking_queue()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), dict(
            state="connected", remaining_orders=3,
            confirm_orders=2, delivery_orders=1,
        ))
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_day_filter_excludes_old_orders_without_changing_historic_total(self):
        rows = [
            ("confirm", "2026-11-22T09:00:00+03:00"),
            ("complete", "2026-11-23T11:00:00+03:00"),
            ("confirm", "2026-11-24T08:00:00+03:00"),
            ("complete", "2026-11-23T21:10:00+00:00"),
            ("confirm", None),
        ]
        with patch.object(wms, "_wb_hint_today", return_value="2026-11-24"), \
                patch.object(wms, "query_all", return_value=rows):
            with self.app.test_request_context("/api/manager/wb-fbs/picking-queue?day=2026-11-24"):
                result = wms.manager_wb_fbs_picking_queue().get_json()
        self.assertEqual(result["day"], "2026-11-24")
        self.assertEqual(result["confirm_orders"], 1)
        self.assertEqual(result["delivery_orders"], 1)
        self.assertEqual(result["remaining_orders"], 2)

    def test_day_filter_rejects_past_day_on_live_endpoint(self):
        with patch.object(wms, "_wb_hint_today", return_value="2026-11-24"):
            with self.app.test_request_context("/api/manager/wb-fbs/picking-queue?day=2026-11-22"):
                response, status = wms.manager_wb_fbs_picking_queue()
        self.assertEqual(status, 400)
        self.assertIn("error", response.get_json())

    def test_empty_queue_is_real_zero(self):
        self.assertEqual(wms.manager_wb_fbs_picking_queue().get_json()["remaining_orders"],0)

    def test_failure_is_not_displayed_as_zero(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TABLE wb_fbs_picking_queue")
        response, code = wms.manager_wb_fbs_picking_queue()
        self.assertEqual(code,503)
        self.assertEqual(response.get_json()["state"],"error")

    def test_manager_required(self):
        with patch.object(wms, "manager_access", return_value=False):
            response, code = wms.manager_wb_fbs_picking_queue()
        self.assertEqual(code,403)
        self.assertIn("error",response.get_json())


if __name__ == "__main__":
    unittest.main()


class WBProgressSourceContractTests(unittest.TestCase):
    """Manager UI only: same Moscow day, separate plan and pending WB queue."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        source = Path("wms.py").read_text(encoding="utf-8")
        cls.ui = source.split("STATS_PAGE = ", 1)[1].split('SHIFT_ADMIN_PAGE = ', 1)[0]

    def test_moscow_day_boundaries_not_browser_local_date(self):
        self.assertIn("new Date(Date.now()+3*3600000).toISOString().slice(0,10)",self.ui)
        self.assertIn("dayKey()+'T00:00:00+03:00'",self.ui)
        self.assertIn("start.getTime()+86400000",self.ui)
        self.assertNotIn("new Date(n.getFullYear(),n.getMonth(),n.getDate())",self.ui)

    def test_history_fact_feeds_dashboard_and_progress(self):
        self.assertIn("/api/manager/integrations/wildberries/history?date=",self.ui)
        self.assertIn("const fact=picked!==null?picked:Number(f.orders||0);",self.ui)
        self.assertIn("document.getElementById('todayOrders').textContent=fact;",self.ui)
        self.assertIn("renderProgress(fact);",self.ui)

    def test_auto_workload_keeps_queue_and_fact_separate(self):
        self.assertIn("const total=n+rest;",self.ui)
        self.assertIn("const pct=total>0?n/total*100:0;",self.ui)
        self.assertIn("Остаток может включать прошлые дни; это не дневной план.",self.ui)
        self.assertIn("renderProgress(fact);",self.ui)
        self.assertNotIn('id="planInput"',self.ui)
        self.assertNotIn('onclick="savePlan()"',self.ui)
        self.assertNotIn("/api/manager/daily-plan?date=",self.ui)
        self.assertNotIn("function savePlan()",self.ui)

    def test_unavailable_wb_queue_never_fakes_zero(self):
        self.assertIn("wbAssemblyCount=null;",self.ui)
        self.assertIn("актуальный остаток WB неизвестен",self.ui)
        self.assertIn("Очередь WB недоступна · общий объём не рассчитан",self.ui)
        self.assertIn("document.getElementById('remaining').textContent='—';",self.ui)

    def test_preserves_live_confirm_delivery_breakdown(self):
        self.assertIn("result.confirm_orders",self.ui)
        self.assertIn("result.delivery_orders",self.ui)
        self.assertIn("обновляется автоматически",self.ui)
