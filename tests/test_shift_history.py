"""Read-only shift history calculations and authorization regression tests."""
import os
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-flask-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TMP.name, "shift-history.db")

from flask import Flask
from wms import ensure_db, execute, query_one, STATS_PAGE
from shift_history import shift_history_bp


class ShiftHistoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.secret_key = "history-test-only"
        cls.app.register_blueprint(shift_history_bp)
        cls.app.testing = True
        ensure_db()

    def setUp(self):
        self.client = self.app.test_client()
        self.employee_id = "history-" + uuid.uuid4().hex
        execute(
            "INSERT INTO employees(id,name,rate,target,active,role) VALUES(?,?,?,?,?,?)",
            (self.employee_id, "Антон, история", 2.25, 1600, 1, "staff"),
        )
        self.authorize()

    def authorize(self):
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["manager_web"] = True

    def insert_shift(self, start, end=None):
        shift_id = str(uuid.uuid4())
        execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,?)",
                (shift_id, self.employee_id, start, end))
        return shift_id

    def insert_activity(self, shift_id, kind, start, end):
        execute(
            "INSERT INTO activities(id,shift_id,employee_id,activity_type,started_at,ended_at)"
            " VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), shift_id, self.employee_id, kind, start, end),
        )

    def insert_scan(self, shift_id, at, debt=False):
        execute(
            "INSERT INTO scans(id,shift_id,employee_id,barcode,scanned_at,scan_type)"
            " VALUES(?,?,?,?,?,?)",
            (str(uuid.uuid4()), shift_id, self.employee_id, "WB-" + uuid.uuid4().hex,
             at, "debt" if debt else "normal"),
        )

    def report(self, start="2026-10-19", end="2026-10-19", employee=True):
        params = {"from": start, "to": end}
        if employee:
            params["employee_id"] = self.employee_id
        return self.client.get("/api/manager/shift-history", query_string=params)

    def test_daily_speed_uses_actual_picking_time_not_full_shift(self):
        sid = self.insert_shift("2026-10-19T06:00:00+00:00",
                                "2026-10-19T15:00:00+00:00")
        self.insert_activity(sid, "picking", "2026-10-19T06:00:00+00:00",
                             "2026-10-19T08:00:00+00:00")
        self.insert_activity(sid, "replenishment", "2026-10-19T08:00:00+00:00",
                             "2026-10-19T08:30:00+00:00")
        self.insert_activity(sid, "picking", "2026-10-19T08:30:00+00:00",
                             "2026-10-19T10:30:00+00:00")
        for n in range(10):
            self.insert_scan(sid, "2026-10-19T07:00:00+00:00", debt=n<2)
        execute(
            "INSERT INTO replenishment_tasks"
            "(id,article,line_code,completed_qty,status,created_at,started_at,"
            "completed_at,created_by,assigned_employee_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "MONACO", "A05", 144, "completed",
             "2026-10-19T08:00:00+00:00", "2026-10-19T08:00:00+00:00",
             "2026-10-19T08:30:00+00:00", "Руководитель", self.employee_id),
        )
        before = query_one("SELECT COUNT(*) FROM scans WHERE employee_id=?", (self.employee_id,))[0]
        r = self.report()
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(len(r.json["rows"]), 1)
        row = r.json["rows"][0]
        self.assertEqual(row["orders"], 10)
        self.assertEqual(row["debt_orders"], 2)
        self.assertEqual(row["picking_seconds"], 4*3600)
        self.assertEqual(row["replenishment_seconds"], 30*60)
        self.assertEqual(row["orders_per_hour"], 2.5)
        self.assertEqual(row["replenishment_tasks"], 1)
        self.assertEqual(row["replenishment_units"], 144)
        self.assertEqual(r.json["totals"]["orders_per_hour"], 2.5)
        self.assertEqual(
            query_one("SELECT COUNT(*) FROM scans WHERE employee_id=?", (self.employee_id,))[0],
            before,
        )

    def test_cross_midnight_shift_is_split_into_local_days(self):
        sid = self.insert_shift("2026-10-19T20:30:00+00:00",
                                "2026-10-19T21:30:00+00:00")
        self.insert_activity(sid, "picking", "2026-10-19T20:30:00+00:00",
                             "2026-10-19T21:30:00+00:00")
        self.insert_scan(sid, "2026-10-19T20:50:00+00:00")
        self.insert_scan(sid, "2026-10-19T21:10:00+00:00")
        r=self.report("2026-10-19","2026-10-20")
        self.assertEqual(r.status_code, 200, r.json)
        rows = {x["date"]: x for x in r.json["rows"]}
        self.assertEqual(set(rows), {"2026-10-19", "2026-10-20"})
        for row in rows.values():
            self.assertEqual(row["orders"], 1)
            self.assertEqual(row["picking_seconds"], 1800)
            self.assertEqual(row["orders_per_hour"], 2.0)
            self.assertEqual(len(row["shifts"]), 1)

    def test_missing_picking_time_reports_unknown_speed(self):
        sid = self.insert_shift("2026-10-19T06:00:00+00:00",
                                "2026-10-19T09:00:00+00:00")
        self.insert_scan(sid, "2026-10-19T07:00:00+00:00")
        r=self.report()
        self.assertEqual(r.status_code, 200)
        row=r.json["rows"][0]
        self.assertIsNone(row["orders_per_hour"])
        self.assertTrue(row["warnings"])
        self.assertIsNone(r.json["totals"]["orders_per_hour"])

    def test_stale_open_shift_has_warning_and_excludes_fictitious_time(self):
        sid = self.insert_shift("2026-10-18T06:00:00+00:00")
        self.insert_activity(sid, "picking", "2026-10-18T06:00:00+00:00", None)
        self.insert_scan(sid, "2026-10-19T07:00:00+00:00")
        r=self.report()
        self.assertEqual(r.status_code, 200, r.json)
        row=r.json["rows"][0]
        self.assertTrue(row["time_unreliable"])
        self.assertEqual(row["picking_seconds"], 0)
        self.assertIsNone(row["orders_per_hour"])
        self.assertTrue(any("16 часов" in warning for warning in row["warnings"]))

    def test_unknown_employee_and_invalid_period_rejected(self):
        r=self.client.get("/api/manager/shift-history", query_string={
            "from":"2026-10-01","to":"2026-11-02"})
        self.assertEqual(r.status_code, 400)
        r=self.client.get("/api/manager/shift-history", query_string={
            "from":"2026-10-19","to":"2026-10-19","employee_id":"no-such-employee"})
        self.assertEqual(r.status_code, 400)

    def test_manager_only_for_page_and_api(self):
        with self.client.session_transaction() as ses:
            ses.clear()
        self.assertEqual(self.client.get("/manager/shift-history").status_code, 302)
        self.assertEqual(self.report().status_code, 403)
        self.authorize()
        r=self.client.get("/manager/shift-history")
        self.assertEqual(r.status_code, 200)
        self.assertIn("История смен и производительность", r.get_data(as_text=True))

    def test_main_manager_page_links_to_history(self):
        self.assertIn('/manager/shift-history', STATS_PAGE)


if __name__ == "__main__":
    unittest.main()
