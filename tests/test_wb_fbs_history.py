"""Synthetic WB FBS history tests. No credentials, warehouse or WB requests."""
import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import wb_fbs_history as history


class WBHistoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE scans (id TEXT, employee_id TEXT, scanned_at TEXT)")
        self.connection.execute("CREATE TABLE employees (id TEXT, name TEXT)")
        self.connection.execute("INSERT INTO employees VALUES ('e1','Тестовый сборщик')")
        @contextmanager
        def mock_connection():
            yield self.connection
        self.patcher = patch.object(history, "db_connect", mock_connection)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        self.connection.close()

    def test_initial_deliver_never_counts_as_today_shipment(self):
        initial = history.record_observed_statuses(
            {100: "deliver", 200: "confirm", 300: "new"},
            "2026-11-16T09:00:00+00:00")
        self.assertEqual(initial, {"first_seen_assembly":1, "first_seen_delivery":0})
        repeat = history.record_observed_statuses(
            {100:"deliver",200:"confirm",300:"new"}, "2026-11-16T09:05:00+00:00")
        self.assertEqual(repeat, {"first_seen_assembly":0, "first_seen_delivery":0})
        move=history.record_observed_statuses(
            {200:"deliver",300:"new"}, "2026-11-16T10:00:00+00:00")
        self.assertEqual(move["first_seen_delivery"], 1)
        again=history.record_observed_statuses(
            {200:"deliver"}, "2026-11-16T10:10:00+00:00")
        self.assertEqual(again["first_seen_delivery"],0)
        records=self.connection.execute(
            "SELECT order_id,delivered_at FROM wb_fbs_status_tracking ORDER BY order_id").fetchall()
        self.assertEqual(records,[("200","2026-11-16T10:00:00+00:00")])

    def test_persistent_daily_report_uses_observed_events_and_real_scan_rows(self):
        date=datetime.now(ZoneInfo("Europe/Moscow")).date()
        event_time=datetime.now(timezone.utc).isoformat(timespec="seconds")
        history.record_observed_statuses({10:"confirm",20:"confirm",30:"deliver"},event_time)
        self.connection.execute("INSERT INTO scans VALUES (?,?,?)",("s1","e1",event_time))
        self.connection.commit()
        history.record_observed_statuses({10:"deliver",20:"confirm"},event_time)
        report=history.daily_history(date.isoformat())
        self.assertEqual(report["new_on_assembly_observed"],2)
        self.assertEqual(report["picked_in_grit"],1)
        self.assertEqual(report["moved_to_delivery_observed"],1)
        self.assertEqual(report["workers"],[{"name":"Тестовый сборщик","picked":1}])

    def test_dont_assume_missing_order_is_shipped(self):
        history.record_observed_statuses({11:"confirm",12:"confirm"},"2026-11-16T08:00:00+00:00")
        history.record_observed_statuses({12:"confirm"},"2026-11-16T09:00:00+00:00")
        rows=self.connection.execute("SELECT COUNT(*) FROM wb_fbs_status_tracking WHERE delivered_at IS NOT NULL").fetchone()
        self.assertEqual(rows[0],0)

    def test_manager_route_and_history_link(self):
        from pathlib import Path
        app=Path("app.py").read_text()
        integrations=Path("integrations.py").read_text()
        wms=Path("wms.py").read_text()
        self.assertIn('"/api/manager/integrations/wildberries/history"',app)
        self.assertIn('"/manager/integrations/wildberries/history"',app)
        self.assertIn('all_statuses = result.pop("order_statuses", None)',integrations)
        self.assertIn('record_observed_statuses(all_statuses)',integrations)
        self.assertIn('История WB FBS · сборка и отгрузка',wms)


if __name__ == "__main__":
    unittest.main()
