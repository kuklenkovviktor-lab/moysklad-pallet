"""Tests for per-operation time totals and the manager shift summary."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-flask-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TMP = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_DB_PATH", os.path.join(_TMP.name, "wms.sqlite"))

from flask import Flask
from wms import (
    activity_seconds_by_type, ensure_db, execute, picking_seconds,
    shift_stats, wms_bp,
)


class OperationTimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()
        cls.app = Flask(__name__)
        cls.app.secret_key = "test"
        cls.app.testing = True
        cls.app.register_blueprint(wms_bp)

    def setUp(self):
        self.shift_id = str(uuid.uuid4())
        self.base = datetime(2026, 10, 19, 8, 0, tzinfo=timezone.utc)
        execute(
            "INSERT INTO shifts (id, employee_id, started_at, ended_at) VALUES (?,?,?,NULL)",
            (self.shift_id, "anton", self.base.isoformat()),
        )

    def add_interval(self, kind, start_m, end_m=None):
        execute(
            "INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at, ended_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), self.shift_id, "anton", kind,
                (self.base + timedelta(minutes=start_m)).isoformat(),
                (self.base + timedelta(minutes=end_m)).isoformat() if end_m is not None else None,
            ),
        )

    def test_repeated_replenishment_is_summed_and_excluded_from_picking(self):
        self.add_interval("picking", 0, 30)
        self.add_interval("replenishment", 30, 40)
        self.add_interval("picking", 40, 70)
        self.add_interval("replenishment", 70, 85)
        self.add_interval("shipping", 85, 90)
        self.add_interval("replenishment", 90)
        at_minute_95 = self.base + timedelta(minutes=95)
        result = activity_seconds_by_type(self.shift_id, now=at_minute_95)
        self.assertEqual(result["picking"], 3600)
        self.assertEqual(result["replenishment"], 1800)
        self.assertEqual(result["shipping"], 300)
        self.assertEqual(picking_seconds(self.shift_id, now=at_minute_95), 3600)

    def test_completed_shift_remains_available_to_manager(self):
        self.add_interval("picking", 0, 30)
        self.add_interval("replenishment", 30, 45)
        end = (self.base + timedelta(minutes=45)).isoformat()
        execute("UPDATE shifts SET ended_at = ? WHERE id = ?", (end, self.shift_id))
        self.assertEqual(shift_stats(self.shift_id)["picking_seconds"], 1800)
        self.assertEqual(shift_stats(self.shift_id)["replenishment_seconds"], 900)

        client = self.app.test_client()
        with client.session_transaction() as s:
            s["manager_web"] = True
        query = {
            "from": (self.base - timedelta(hours=1)).isoformat(),
            "to": (self.base + timedelta(hours=3)).isoformat(),
        }
        response = client.get("/api/fbs/dashboard", query_string=query)
        self.assertEqual(response.status_code, 200, response.json)
        completed = [w for w in response.json["completed"] if w["shift_id"] == self.shift_id]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["replenishment_seconds"], 900)
        self.assertEqual(completed[0]["picking_seconds"], 1800)


if __name__ == "__main__":
    unittest.main()
