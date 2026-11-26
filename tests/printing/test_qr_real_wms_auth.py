"""REAL existing GRIT WMS session auth + synthetic QR/PG17 print queue.

Employee and shift identity come from the unmodified wms.get_session_employee()
using a disposable SQLite WMS database. Order/catalog/QR/print-job fixtures
live in a SEPARATE disposable PostgreSQL17 instance, never production.
No printer I/O, WB API or production app registration.
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

from services.printing.pg_queue import initialize_synthetic_print_pg
from services.printing.qr_scan import initialize_synthetic_stickers
from services.printing.qr_http_synthetic import (
    make_real_wms_synthetic_autoqueue_blueprint,
)

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PG test gate required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Unapproved synthetic PostgreSQL URL")

ROOT = Path(__file__).resolve().parents[2]
fixture_spec = importlib.util.spec_from_file_location(
    "pr192_wms_qr_integrated_fixture",
    ROOT / "tests/test_line_printer_pg_integrated_trial.py")
fixture_module = importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture_module)


def pg():
    return psycopg.connect(DSN)


class RealWMSAutoqueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        isolated_env = patch.dict(os.environ, {
            "WMS_BADGE_SECRET": "pr192-isolated-wms-badge-only",
            "WMS_TERMINAL_SECRET": "pr192-isolated-wms-terminal-only",
            "DATABASE_URL": "",
            "WMS_REQUIRE_POSTGRES": "0",
            "WMS_DB_PATH": str(Path(cls.tmp.name) / "synthetic-only-wms.sqlite"),
        })
        isolated_env.start()
        cls.addClassCleanup(isolated_env.stop)

        wms_spec = importlib.util.spec_from_file_location(
            "pr192_qr_real_wms_auth", ROOT / "wms.py")
        cls.wms = importlib.util.module_from_spec(wms_spec)
        wms_spec.loader.exec_module(cls.wms)
        module_patch = patch.dict(sys.modules, {"wms": cls.wms})
        module_patch.start()
        cls.addClassCleanup(module_patch.stop)
        cls.wms.init_wms_db()

        fixture_module.IntegratedPrinterPGTests.setUpClass()
        with pg() as conn:
            initialize_synthetic_print_pg(conn)
            initialize_synthetic_stickers(conn)

    def setUp(self):
        with pg() as conn:
            conn.execute("DELETE FROM synthetic_pr192_print_events")
            conn.execute("DELETE FROM synthetic_pr192_print_jobs")
            conn.execute("DELETE FROM synthetic_pr192_print_stickers")
        fixture = fixture_module.IntegratedPrinterPGTests(
            "test_valid_full_path_first_reservation")
        fixture.setUp()
        self.qr = fixture.payload
        with pg() as conn:
            conn.execute("""INSERT INTO synthetic_pr192_print_stickers
              (account_id,order_id,original_sticker)
              VALUES ('synthetic','o1','SYNTH-WB-STICKER-1')""")

        with self.wms.db_connect() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM shifts WHERE employee_id IN ('e1','e2')")
            cur.execute("""INSERT INTO employees(id,name,role,active)
                VALUES('e1','Демо-сотрудник','staff',1)
                ON CONFLICT(id) DO UPDATE SET active=1""")
            cur.execute("""INSERT INTO employees(id,name,role,active)
                VALUES('e2','Демо-сотрудник 2','staff',1)
                ON CONFLICT(id) DO UPDATE SET active=1""")
            cur.execute("""INSERT INTO shifts(id,employee_id,started_at,ended_at)
                VALUES('s1','e1','2026-11-26T10:00:00',NULL),
                      ('s2','e2','2026-11-26T10:00:00',NULL)""")
            conn.commit()
            cur.close()

        app = Flask(__name__)
        app.secret_key = "pr192-isolated-session-secret"
        app.register_blueprint(
            make_real_wms_synthetic_autoqueue_blueprint(pg))
        self.app = app
        self.worker = app.test_client()
        self.manager = app.test_client()
        self.other = app.test_client()
        with self.worker.session_transaction() as s:
            s.update(employee_id="e1", employee_shift_id="s1",
                     attendance_verified=True,
                     pr192_print_csrf="isolated-csrf-token-123456")
        with self.other.session_transaction() as s:
            s.update(employee_id="e2", employee_shift_id="s2",
                     attendance_verified=True,
                     pr192_print_csrf="isolated-csrf-token-123456")
        with self.manager.session_transaction() as s:
            s.update(manager_web=True,
                     pr192_print_csrf="isolated-csrf-token-123456")

    def scan(self, client=None, **updates):
        data = {"qr": self.qr, "csrf": "isolated-csrf-token-123456"}
        data.update(updates)
        return (client or self.worker).post(
            "/synthetic/pr192/print-queue/scan", json=data)

    def count(self):
        with pg() as conn:
            return conn.execute(
                "SELECT count(*) FROM synthetic_pr192_print_jobs").fetchone()[0]

    def test_real_wms_authorizes_qr_and_autoqueue(self):
        r = self.scan()
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(r.json["queued_new"], 1)
        self.assertFalse(r.json["physical_printing"])
        self.assertEqual(self.count(), 1)

    def test_real_wms_auth_rescan_is_idempotent(self):
        self.assertEqual(self.scan().json["queued_new"],1)
        other = self.scan()
        self.assertEqual(other.status_code, 200, other.json)
        self.assertEqual(other.json["queued_new"],0)
        self.assertEqual(other.json["already_queued"],1)
        self.assertEqual(self.count(),1)

    def test_closed_shift_in_real_wms_revokes_session(self):
        with self.wms.db_connect() as conn:
            conn.execute("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
            conn.commit()
        self.assertEqual(self.scan().status_code,401)
        self.assertEqual(self.count(),0)

    def test_real_wms_deactivated_employee_denied(self):
        with self.wms.db_connect() as conn:
            conn.execute("UPDATE employees SET active=0 WHERE id='e1'")
            conn.commit()
        self.assertEqual(self.scan().status_code,401)
        self.assertEqual(self.count(),0)

    def test_wrong_shift_binding_is_rejected(self):
        with self.worker.session_transaction() as s:
            s["employee_shift_id"]="s2"
        self.assertEqual(self.scan().status_code,401)
        self.assertEqual(self.count(),0)

    def test_employee_json_spoofing_denied(self):
        self.assertEqual(self.scan(employee_id="e2").status_code,400)
        self.assertEqual(self.scan(shift_id="s2").status_code,400)
        self.assertEqual(self.scan(order_id="o1").status_code,400)
        self.assertEqual(self.count(),0)

    def test_manager_session_cannot_scan_as_worker(self):
        self.assertEqual(self.scan(self.manager).status_code,401)
        self.assertEqual(self.count(),0)

    def test_real_wms_auth_does_not_bypass_pg_picking_gate(self):
        with pg() as conn:
            conn.execute("UPDATE activities SET ended_at='closed' WHERE shift_id='s1'")
        self.assertEqual(self.scan().status_code,403)
        self.assertEqual(self.count(),0)

    def test_real_wms_auth_does_not_bypass_pg_badge_gate(self):
        with pg() as conn:
            conn.execute("UPDATE employees SET active=0 WHERE id='e1'")
        self.assertEqual(self.scan().status_code,403)
        self.assertEqual(self.count(),0)

    def test_real_wms_auth_does_not_bypass_qr_revocation(self):
        with pg() as conn:
            conn.execute("UPDATE grit_line_printers SET enabled=0")
        self.assertEqual(self.scan().status_code,403)
        self.assertEqual(self.count(),0)

    def test_csrf_is_required_even_with_valid_wms_session(self):
        self.assertEqual(self.scan(csrf="wrong").status_code,403)
        self.assertEqual(self.count(),0)

    def test_unverified_attendance_denied(self):
        with self.worker.session_transaction() as s:
            s.pop("attendance_verified",None)
        self.assertEqual(self.scan().status_code,401)
        self.assertEqual(self.count(),0)

    def test_second_employee_wms_session_works_without_duplicate(self):
        self.assertEqual(self.scan().json["queued_new"],1)
        r = self.scan(self.other)
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(r.json["already_queued"],1)
        self.assertEqual(self.count(),1)

    def test_real_wms_factory_refuses_without_isolated_gate(self):
        with patch.dict(os.environ, {"GRIT_SYNTHETIC_PG_TEST":"0"}):
            with self.assertRaisesRegex(RuntimeError,"disabled"):
                make_real_wms_synthetic_autoqueue_blueprint(pg)


if __name__ == "__main__":
    unittest.main()
