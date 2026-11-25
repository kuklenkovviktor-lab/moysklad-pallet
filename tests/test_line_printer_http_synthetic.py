"""Synthetic HTTP integration tests: Flask sessions + PostgreSQL 17 lifecycle."""
import importlib.util
import os
from pathlib import Path
import unittest
from flask import Flask, session

import psycopg

from line_printer_http_synthetic import make_synthetic_printer_blueprint

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Synthetic PG test flag required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing external database")

spec = importlib.util.spec_from_file_location(
    "synthetic_integrated_fixture",
    Path(__file__).resolve().parent / "test_line_printer_pg_integrated_trial.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)

from line_printer_pg_lifecycle import initialize_lifecycle


def connect():
    return psycopg.connect(DSN)


def authenticated_employee():
    # Synthetic stand-in for GRIT's real wms.get_session_employee().
    eid = session.get("employee_id")
    if not eid:
        return None
    with connect() as conn:
        row = conn.execute("SELECT id FROM employees WHERE id=%s AND active=1",
                           (eid,)).fetchone()
    return {"id": row[0]} if row else None


def manager_is_authorized():
    return session.get("manager_web") is True and bool(session.get("pr192_manager_id"))


class SyntheticHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.IntegratedPrinterPGTests.setUpClass()
        with connect() as conn:
            initialize_lifecycle(conn)

    def setUp(self):
        fixture = fixture_module.IntegratedPrinterPGTests("test_valid_full_path_first_reservation")
        fixture.setUp()
        self.qr = fixture.payload
        with connect() as conn:
            conn.execute("DELETE FROM synthetic_line_printer_lifecycle_events")
            conn.execute("DELETE FROM synthetic_line_printer_lifecycle")
        app = Flask(__name__)
        app.secret_key = "synthetic-only-http-secret"
        app.register_blueprint(make_synthetic_printer_blueprint(
            connect, authenticated_employee, manager_is_authorized))
        self.app = app
        self.worker = app.test_client()
        self.other = app.test_client()
        self.manager = app.test_client()
        with self.worker.session_transaction() as sess:
            sess.update(employee_id="e1", employee_shift_id="s1",
                        attendance_verified=True)
        with self.other.session_transaction() as sess:
            sess.update(employee_id="e2", employee_shift_id="s2",
                        attendance_verified=True)
        with self.manager.session_transaction() as sess:
            sess.update(manager_web=True, pr192_manager_id="boss",
                        pr192_manager_csrf="synthetic-csrf")

    def reserve(self, client=None, **overrides):
        data = {"order_id": "o1", "printer_qr": self.qr,
                "reservation_id": "lease1", "action_id": "reserve1"}
        data.update(overrides)
        return (client or self.worker).post("/synthetic/pr192/reserve", json=data)

    def complete(self, client=None, **overrides):
        data = {"order_id": "o1", "reservation_id": "lease1",
                "action_id": "complete1"}
        data.update(overrides)
        return (client or self.worker).post("/synthetic/pr192/complete", json=data)

    def release(self, client=None, **overrides):
        data = {"order_id": "o1", "reservation_id": "lease1",
                "action_id": "release1",
                "reason": "Руководитель проверил недостачу",
                "csrf": "synthetic-csrf"}
        data.update(overrides)
        return (client or self.manager).post("/synthetic/pr192/release", json=data)

    def state(self):
        return self.manager.get("/synthetic/pr192/status/o1").json["state"]

    def audit(self):
        return self.manager.get("/synthetic/pr192/audit/o1").json["events"]

    def test_full_reserve_release_and_reassignment_audited(self):
        self.assertEqual(self.state(), "FREE")
        self.assertEqual(self.reserve().json["state"], "RESERVED")
        self.assertEqual(self.release().json["state"], "RELEASED")
        self.assertEqual(self.reserve(self.other, action_id="reserve2",
                                      reservation_id="lease2").json["state"], "RESERVED")
        self.assertEqual([event["action"] for event in self.audit()],
                         ["RESERVE", "RELEASE", "RESERVE"])

    def test_complete_requires_existing_wb_scan(self):
        self.reserve()
        denied = self.complete()
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.state(), "RESERVED")
        with connect() as conn:
            conn.execute("UPDATE wb_fbs_orders SET local_status='complete' WHERE order_id='o1'")
        self.assertEqual(self.complete().json["state"], "COMPLETED")
        self.assertEqual(self.state(), "COMPLETED")
        self.assertEqual(self.release().status_code, 409)

    def test_worker_cannot_release_or_see_manager_audit(self):
        self.reserve()
        self.assertEqual(self.release(self.worker).status_code, 403)
        self.assertEqual(self.worker.get("/synthetic/pr192/audit/o1").status_code, 403)
        self.assertEqual(self.worker.get("/synthetic/pr192/status/o1").status_code, 403)
        self.assertEqual(self.state(), "RESERVED")

    def test_manager_cannot_replay_as_worker(self):
        self.assertEqual(self.reserve(self.manager).status_code, 401)
        self.assertEqual(self.state(), "FREE")

    def test_unbound_terminal_and_spoofed_employee_rejected(self):
        with self.worker.session_transaction() as sess:
            sess.pop("employee_shift_id")
        self.assertEqual(self.reserve().status_code, 401)
        with self.worker.session_transaction() as sess:
            sess["employee_shift_id"] = "s1"
        self.assertEqual(self.reserve(employee_id="e2").status_code, 400)
        self.assertEqual(self.state(), "FREE")

    def test_manager_csrf_required(self):
        self.reserve()
        self.assertEqual(self.release(csrf="bad").status_code, 403)
        self.assertEqual(self.state(), "RESERVED")

    def test_release_refuses_if_order_already_completed_in_wb(self):
        self.reserve()
        with connect() as conn:
            conn.execute("UPDATE wb_fbs_orders SET local_status='complete'")
        self.assertEqual(self.release().status_code, 403)
        self.assertEqual(self.state(), "RESERVED")

    def test_revoked_qr_prevents_initial_reservation(self):
        with connect() as conn:
            conn.execute("UPDATE grit_line_printers SET enabled=0")
        self.assertEqual(self.reserve().status_code, 403)
        self.assertEqual(self.state(), "FREE")

    def test_invalid_session_after_employee_disabled(self):
        with connect() as conn:
            conn.execute("UPDATE employees SET active=0 WHERE id='e1'")
        self.assertEqual(self.reserve().status_code, 401)
        self.assertEqual(self.state(), "FREE")

    def test_action_replay_does_not_change_state(self):
        self.assertEqual(self.reserve().status_code, 200)
        self.assertEqual(self.reserve().status_code, 409)
        self.assertEqual(len(self.audit()), 1)


if __name__ == "__main__":
    unittest.main()
