"""Tests for ONIX first-barcode timing diagnostics."""
import os
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-flask-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
tmp = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(tmp.name, "wms.sqlite")

from flask import Flask
from wms import ensure_db, query_one
from fbs_diagnostics import diag_bp


class DiagnosticsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()
        cls.app = Flask(__name__)
        cls.app.secret_key = "test"
        cls.app.register_blueprint(diag_bp)
        cls.app.testing = True

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["employee_id"] = "anton"

    def payload(self, **overrides):
        p = {"id": str(uuid.uuid4()), "zone": "Линия М", "network": "Wi-Fi 2,4 ГГц",
             "total_ms": 1200, "server_ms": 100, "http_status": 200, "ok": True}
        p.update(overrides)
        return p

    def manager(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["manager_web"] = True
        return c

    def test_duplicate_measurement_does_not_create_orders(self):
        initial = query_one("SELECT COUNT(*) FROM scans")[0]
        p = self.payload()
        self.assertEqual(self.client.post("/api/fbs/diagnostics", json=p).status_code, 200)
        self.assertEqual(self.client.post("/api/fbs/diagnostics", json=p).status_code, 200)
        self.assertEqual(initial, query_one("SELECT COUNT(*) FROM scans")[0])
        api = self.manager().get("/api/manager/fbs-diagnostics")
        self.assertEqual(api.status_code, 200, api.json)
        self.assertTrue(any(x["id"] if "id" in x else x["total_ms"] == 1200 for x in api.json["recent"]))

    def test_zone_summary_and_failed_scan(self):
        p = self.payload(zone="Линия А")
        for kw in ({}, {"total_ms": 400, "server_ms": 100}, 
                   {"total_ms": 2000, "server_ms": 0, "http_status": 0, "ok": False}):
            r = self.client.post("/api/fbs/diagnostics", json=self.payload(zone="Линия А", **kw))
            self.assertEqual(r.status_code, 200, r.json)
        data = self.manager().get("/api/manager/fbs-diagnostics").json
        row = next(x for x in data["groups"] if x["zone"] == "Линия А")
        self.assertEqual(row["samples"], 3)
        self.assertEqual(row["ok"], 2)
        self.assertEqual(row["errors"], 1)
        self.assertEqual(row["avg_ms"], 800)
        self.assertEqual(row["avg_server_ms"], 100)
        self.assertEqual(row["avg_other_ms"], 700)

    def test_invalid_inputs_fail(self):
        for bad in ({"zone": "неизвестная"}, {"server_ms": 120000},
                    {"total_ms": -1}, {"total_ms": "NaN"}, {"id": "x"}):
            self.assertEqual(self.client.post("/api/fbs/diagnostics", json=self.payload(**bad)).status_code, 400)

    def test_staff_cannot_read_manager_report(self):
        self.assertEqual(self.client.get("/api/manager/fbs-diagnostics").status_code, 403)

    def test_worker_auth_required_to_write(self):
        self.assertEqual(self.app.test_client().post("/api/fbs/diagnostics",json=self.payload()).status_code, 401)


if __name__ == "__main__":
    unittest.main()
