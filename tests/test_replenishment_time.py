"""Regression tests for independent replenishment timer; no live credentials."""
import os
import tempfile
import unittest

TESTDIR = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(TESTDIR.name, "onix-test.db")
os.environ["WMS_BADGE_SECRET"] = "test-badge-secret-only"
os.environ["WMS_TERMINAL_SECRET"] = "test-terminal-secret-only"
os.environ.pop("DATABASE_URL", None)
os.environ.pop("WMS_REQUIRE_POSTGRES", None)

from flask import Flask
import wms
from replenishment_time import timer_bp


class TimerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wms.ensure_db()

    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test-session"
        self.app.register_blueprint(timer_bp)
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s["employee_id"] = "anton"
        self.sid, _ = wms.ensure_active_shift("anton", "picking")

    def tearDown(self):
        # Keep unit tests isolated even if runner uses same process.
        wms.execute("DELETE FROM replenishment_trip_times WHERE employee_id = ?", ("anton",))
        wms.execute("DELETE FROM activities WHERE employee_id = ?", ("anton",))
        wms.execute("DELETE FROM shifts WHERE employee_id = ?", ("anton",))

    def test_start_repeat_finish_state(self):
        start = self.client.post("/api/replenishment-time/start", json={"location":"М4"})
        self.assertEqual(start.status_code, 200, start.get_json())
        self.assertEqual(start.get_json()["active"]["location"], "М4")
        self.assertEqual(wms.current_activity(self.sid)["type"], "replenishment")
        second = self.client.post("/api/replenishment-time/start", json={"location":"М5"})
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["resumed"])
        self.assertEqual(second.get_json()["active"]["location"], "М4")
        restarted = self.client.get("/api/replenishment-time/state").get_json()
        self.assertIsNotNone(restarted["active"])
        finish = self.client.post("/api/replenishment-time/finish", json={"barcode":"ONIX-RESERVE-3"})
        self.assertEqual(finish.status_code, 200, finish.get_json())
        self.assertEqual(finish.get_json()["trip"]["reserve"], "3")
        self.assertEqual(wms.current_activity(self.sid)["type"], "picking")
        state = self.client.get("/api/replenishment-time/state").get_json()
        self.assertEqual(state["trips"], 1)
        self.assertIsNone(state["active"])
        self.assertEqual(self.client.post("/api/replenishment-time/finish",
                          json={"barcode":"ONIX-RESERVE-3"}).status_code, 409)

    def test_cancel_does_not_add_completed_trip(self):
        self.assertEqual(self.client.post("/api/replenishment-time/start",
                                          json={"location":"И"}).status_code, 200)
        self.assertEqual(self.client.post("/api/replenishment-time/cancel", json={}).status_code, 200)
        state = self.client.get("/api/replenishment-time/state").get_json()
        self.assertEqual(state["trips"], 0)
        self.assertIsNone(state["active"])
        self.assertEqual(wms.current_activity(self.sid)["type"], "picking")

    def test_finish_requires_reserve_qr(self):
        self.client.post("/api/replenishment-time/start", json={"location":"Ж1"})
        self.assertEqual(self.client.post("/api/replenishment-time/finish",
                          json={"barcode":"something-else"}).status_code, 400)
        self.assertIsNotNone(self.client.get("/api/replenishment-time/state").get_json()["active"])


if __name__ == "__main__":
    unittest.main()
