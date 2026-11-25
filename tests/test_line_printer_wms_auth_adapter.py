"""REAL GRIT WMS auth + isolated PostgreSQL17 synthetic HTTP acceptance tests.

This exercises unmodified wms.get_session_employee() and manager_access(),
backed by disposable SQLite WMS records, never production credentials.
Order/QR/lifecycle records remain in a separate disposable PostgreSQL17 DB.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import psycopg
from flask import Flask

from line_printer_wms_auth_adapter import make_real_wms_auth_synthetic_blueprint
from line_printer_pg_lifecycle import initialize_lifecycle

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Synthetic PostgreSQL gate required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing unsanctioned database URL")

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fixture_line_printer_integrated", ROOT / "tests/test_line_printer_pg_integrated_trial.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


def connect_pg():
    return psycopg.connect(DSN)


class RealWMSAuthHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        env = patch.dict(os.environ, {
            "WMS_BADGE_SECRET": "pr192-synthetic-only-badge",
            "WMS_TERMINAL_SECRET": "pr192-synthetic-only-terminal",
            "DATABASE_URL": "",
            "WMS_REQUIRE_POSTGRES": "0",
            "WMS_DB_PATH": str(Path(cls.tmp.name) / "synthetic-wms-auth.db"),
        })
        env.start()
        cls.addClassCleanup(env.stop)
        spec_wms = importlib.util.spec_from_file_location(
            "wms_pr192_synthetic_real_auth", ROOT / "wms.py")
        cls.wms = importlib.util.module_from_spec(spec_wms)
        spec_wms.loader.exec_module(cls.wms)
        module_patch = patch.dict(sys.modules, {"wms": cls.wms})
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.wms.init_wms_db()
        fixture_module.IntegratedPrinterPGTests.setUpClass()
        with connect_pg() as conn:
            initialize_lifecycle(conn)

    def setUp(self):
        fixture = fixture_module.IntegratedPrinterPGTests("test_valid_full_path_first_reservation")
        fixture.setUp()
        self.qr = fixture.payload
        with connect_pg() as conn:
            conn.execute("DELETE FROM scans")
            conn.execute("DELETE FROM synthetic_line_printer_lifecycle_events")
            conn.execute("DELETE FROM synthetic_line_printer_lifecycle")
        with self.wms.db_connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM shifts WHERE employee_id IN ('e1','e2')")
            cur.execute("""
                INSERT INTO employees (id,name,role,active)
                VALUES ('e1','Синтетический Е1','staff',1)
                ON CONFLICT(id) DO UPDATE SET active=1
            """)
            cur.execute("""
                INSERT INTO employees (id,name,role,active)
                VALUES ('e2','Синтетический Е2','staff',1)
                ON CONFLICT(id) DO UPDATE SET active=1
            """)
            cur.execute("""
                INSERT INTO shifts(id,employee_id,started_at,ended_at)
                VALUES ('s1','e1','2026-11-25T09:00:00',NULL),
                       ('s2','e2','2026-11-25T09:00:00',NULL)
            """)
            conn.commit()
            cur.close()
        app = Flask(__name__)
        app.secret_key = "pr192-synthetic-signed-cookie"
        app.register_blueprint(make_real_wms_auth_synthetic_blueprint(connect_pg))
        self.app = app
        self.worker = app.test_client()
        self.manager = app.test_client()
        with self.worker.session_transaction() as ses:
            ses.update(employee_id="e1", employee_shift_id="s1",
                       attendance_verified=True)
        with self.manager.session_transaction() as ses:
            ses.update(manager_web=True, pr192_manager_id="synthetic-boss",
                       pr192_manager_csrf="synthetic-csrf")

    def reserve(self, client=None, **overrides):
        data = {"order_id": "o1", "printer_qr": self.qr,
                "reservation_id": "lease1", "action_id": "reserve1"}
        data.update(overrides)
        return (client or self.worker).post("/synthetic/pr192/reserve", json=data)

    def state(self):
        return self.manager.get("/synthetic/pr192/status/o1").json["state"]

    def test_real_wms_session_reserves_synthetic_order(self):
        r = self.reserve()
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(self.state(), "RESERVED")

    def test_real_wms_closed_shift_revokes_old_cookie(self):
        with self.wms.db_connect() as conn:
            conn.execute("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
            conn.commit()
        self.assertEqual(self.reserve().status_code, 401)
        self.assertEqual(self.state(), "FREE")

    def test_real_wms_deactivated_badge_denied(self):
        with self.wms.db_connect() as conn:
            conn.execute("UPDATE employees SET active=0 WHERE id='e1'")
            conn.commit()
        self.assertEqual(self.reserve().status_code, 401)
        self.assertEqual(self.state(), "FREE")

    def test_client_supplied_employee_is_not_trusted(self):
        r = self.reserve(employee_id="e2", shift_id="s2")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.state(), "FREE")

    def test_real_wms_manager_access_and_csrf_release(self):
        self.assertEqual(self.reserve().status_code, 200)
        forbidden = self.worker.post("/synthetic/pr192/release", json={
            "order_id": "o1", "reservation_id": "lease1",
            "action_id": "release1", "reason": "synthetic reason",
            "csrf": "synthetic-csrf"})
        self.assertEqual(forbidden.status_code, 403)
        allowed = self.manager.post("/synthetic/pr192/release", json={
            "order_id": "o1", "reservation_id": "lease1",
            "action_id": "release1", "reason": "Руководитель проверил",
            "csrf": "synthetic-csrf"})
        self.assertEqual(allowed.status_code, 200, allowed.json)
        self.assertEqual(self.state(), "RELEASED")

    def test_adapter_refuses_without_synthetic_test_flag(self):
        with patch.dict(os.environ, {"GRIT_SYNTHETIC_PG_TEST": "0"}):
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                make_real_wms_auth_synthetic_blueprint(connect_pg)


if __name__ == "__main__":
    unittest.main()
