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
        # Production route reads request.args; direct handler tests need a request context.
        self.req = self.app.test_request_context("/api/manager/wb-fbs/picking-queue")
        self.req.push()
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
                    local_status TEXT, created_at TEXT, synced_at TEXT,
                    PRIMARY KEY(account_id,order_id));
                CREATE TABLE wb_fbs_picking_queue (
                    account_id TEXT, order_id TEXT, accepted_at TEXT,
                    PRIMARY KEY(account_id,order_id));
            """)

    def tearDown(self):
        self.enabled.stop()
        self.ensure.stop()
        self.access.stop()
        self.req.pop()
        self.ctx.pop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def seed(self, account, oid, wb_status, local_status, admitted,
             created="2026-11-24T08:00:00Z", accepted="2026-11-25T07:00:00Z"):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO wb_fbs_orders VALUES(?,?,?,?,?,?)",
                         (account,oid,wb_status,local_status,created,
                          "2026-11-25T08:00:00Z"))
            if admitted:
                conn.execute("INSERT INTO wb_fbs_picking_queue VALUES(?,?,?)",
                             (account,oid,accepted))

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
        data=response.get_json()
        self.assertEqual(data["state"],"connected")
        self.assertEqual(data["source"],"wb_api_synced_statuses")
        self.assertEqual(data["assembly_orders"],3)
        self.assertEqual(data["assembled_assembly_orders"],1)
        self.assertEqual(data["remaining_orders"],2)
        self.assertEqual(data["confirm_orders"],2)
        self.assertEqual(data["delivery_orders"],1)
        self.assertEqual(data["unadmitted_orders"],0)
        self.assertIsNone(data["day"])
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_current_confirm_ignores_creation_day_and_tracks_unadmitted(self):
        self.seed("default","old","confirm","pending",True,
                  created="2026-11-24T02:00:00Z",accepted="2026-11-25T06:00:00Z")
        self.seed("default","today","confirm","pending",True,
                  created="2026-11-25T05:00:00Z",accepted="2026-11-25T08:00:00Z")
        self.seed("default","outside","confirm","pending",False,
                  created="2026-11-23T05:00:00Z")
        self.seed("default","assembled","confirm","complete",True)
        self.seed("default","shipped","complete","pending",True)
        data=wms.manager_wb_fbs_picking_queue().get_json()
        self.assertEqual(data["assembly_orders"],4)
        self.assertEqual(data["assembled_assembly_orders"],1)
        self.assertEqual(data["remaining_orders"],3)
        self.assertEqual(data["unadmitted_orders"],1)
        self.assertEqual(data["delivery_orders"],1)

    def test_assembly_admission_day_not_created_day(self):
        self.seed("default","old","confirm","pending",True,
                  created="2026-11-23T10:00:00Z",
                  accepted="2026-11-24T23:10:00Z")
        self.seed("default","today","confirm","pending",True,
                  created="2026-11-25T10:00:00Z",
                  accepted="2026-11-25T11:00:00Z")
        with self.app.test_request_context("/api/manager/wb-fbs/picking-queue?day=2026-11-25"):
            data=wms.manager_wb_fbs_picking_queue().get_json()
        self.assertEqual(data["assembly_orders"],2)  # Moscow: 23:10Z is Nov25
        with self.app.test_request_context("/api/manager/wb-fbs/picking-queue?day=2026-11-24"):
            data=wms.manager_wb_fbs_picking_queue().get_json()
        self.assertEqual(data["assembly_orders"],0)

    def test_invalid_admission_day_rejected(self):
        with self.app.test_request_context("/api/manager/wb-fbs/picking-queue?day=2026-02-30"):
            response, status = wms.manager_wb_fbs_picking_queue()
        self.assertEqual(status,400)
        self.assertIn("error",response.get_json())

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


class WBWorkerStatusUIContractTests(unittest.TestCase):
    """TSD renders server-provided WB status without altering scan rules."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        source = Path("wms.py").read_text(encoding="utf-8")
        cls.ui = source.split('FBS_PAGE = """', 1)[1].split('"""', 1)[0]

    def test_cancel_is_red_full_screen_and_titled(self):
        self.assertIn(".tsd-error{position:fixed",self.ui)
        self.assertIn("background:#b42318",self.ui)
        self.assertIn("reason==='WB_CANCELLED'?'ЗАКАЗ ОТМЕНЕН'",self.ui)
        self.assertIn("showTsdError(d.error||'Ошибка ШК WB',d.reason)",self.ui)

    def test_delivery_is_distinct_warning_only_on_confirmed_scan(self):
        self.assertIn("d.wb_status==='complete'?'#ffd166':''",self.ui)
        self.assertIn("ЗАКАЗ В ДОСТАВКЕ WB · скан GRIT успешно подтверждён",self.ui)
        self.assertIn("d.replayed?'Заказ уже учтён ранее",self.ui)

    def test_other_errors_have_distinct_titles(self):
        for label in ("ЗАКАЗ УЖЕ СОБРАН","НЕ ТОТ ТОВАР","НЕ НА СБОРКЕ",
                      "СТАТУС WB УСТАРЕЛ"):
            self.assertIn(label,self.ui)


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
        self.assertIn("const rest=wbAssemblyCount.remaining_orders;",self.ui)
        self.assertIn("const done=wbAssemblyCount.assembled_assembly_orders;",self.ui)
        self.assertIn("const total=wbAssemblyCount.assembly_orders;",self.ui)
        self.assertIn("const pct=total>0?done/total*100:0;",self.ui)
        self.assertIn("fetchWithTimeout('/api/manager/wb-fbs/picking-queue',{cache:'no-store'},5000)",self.ui)
        self.assertNotIn("/api/manager/wb-fbs/picking-queue?day=",self.ui)
        self.assertNotIn("renderProgress(fact);",self.ui)
        self.assertIn("renderProgress();",self.ui)
        self.assertNotIn('id="planInput"',self.ui)
        self.assertNotIn('onclick="savePlan()"',self.ui)
        self.assertNotIn("/api/manager/daily-plan?date=",self.ui)
        self.assertNotIn("function savePlan()",self.ui)

    def test_unavailable_wb_queue_never_fakes_zero(self):
        self.assertIn("wbAssemblyCount=null;",self.ui)
        self.assertIn("Текущий статус WB не получен · прогресс неизвестен",self.ui)
        self.assertIn("Очередь WB недоступна · актуальный объём не рассчитан",self.ui)
        self.assertIn("document.getElementById('remaining').textContent='—';",self.ui)

    def test_preserves_live_confirm_delivery_breakdown(self):
        self.assertIn("result.assembly_orders",self.ui)
        self.assertIn("result.delivery_orders",self.ui)
        self.assertIn("result.unadmitted_orders",self.ui)
        self.assertIn("обновляется автоматически",self.ui)
