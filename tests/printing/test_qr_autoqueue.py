"""Synthetic QR-only Flask -> PostgreSQL 17 automatic queue tests.

Test fixture uses invented WB orders and sticker values in a disposable DB.
The existing real GRIT app is NOT started and nothing is printed.
"""
import importlib.util
import os
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg
from flask import Flask, session

from line_printer_registry import create_printer
from services.printing.pg_queue import (
    initialize_synthetic_print_pg, read_synthetic_print_jobs)
from services.printing.qr_scan import (
    initialize_synthetic_stickers, scan_synthetic_printer_qr)
from services.printing.qr_http_synthetic import make_synthetic_autoqueue_blueprint

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PostgreSQL flag required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Unapproved synthetic DSN")

spec = importlib.util.spec_from_file_location(
    "synthetic_integrated_printer_fixture",
    Path(__file__).resolve().parents[1] /
    "test_line_printer_pg_integrated_trial.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


def connect():
    return psycopg.connect(DSN)


def authenticated_employee():
    # Simulated session identity for this synthetic HTTP unit test.
    # The real integration binds this to wms.get_session_employee.
    eid = session.get("employee_id")
    return {"id": eid} if isinstance(eid, str) else None


class QRQueueHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.IntegratedPrinterPGTests.setUpClass()
        with connect() as conn:
            initialize_synthetic_print_pg(conn)
            initialize_synthetic_stickers(conn)

    def setUp(self):
        with connect() as conn:
            conn.execute("DELETE FROM synthetic_pr192_print_events")
            conn.execute("DELETE FROM synthetic_pr192_print_jobs")
            conn.execute("DELETE FROM synthetic_pr192_print_stickers")
        fixture = fixture_module.IntegratedPrinterPGTests("test_valid_full_path_first_reservation")
        fixture.setUp()
        self.qr = fixture.payload
        with connect() as conn:
            conn.execute("""INSERT INTO synthetic_pr192_print_stickers
              (account_id,order_id,original_sticker)
              VALUES ('synthetic','o1','TEST-ONLY-ORIGINAL-STICKER-1')""")
        app = Flask(__name__)
        app.secret_key = "pr192-synthetic-http-secret"
        app.register_blueprint(
            make_synthetic_autoqueue_blueprint(connect, authenticated_employee))
        self.worker = app.test_client()
        self.manager = app.test_client()
        with self.worker.session_transaction() as s:
            s.update(employee_id="e1", employee_shift_id="s1",
                     attendance_verified=True, pr192_print_csrf="synthetic-test-csrf")
        with self.manager.session_transaction() as s:
            s.update(manager_web=True, pr192_print_csrf="synthetic-test-csrf")

    def scan(self, client=None, **override):
        data = {"qr": self.qr, "csrf": "synthetic-test-csrf"}
        data.update(override)
        return (client or self.worker).post(
            "/synthetic/pr192/print-queue/scan", json=data)

    def job_count(self):
        with connect() as conn:
            return conn.execute(
                "SELECT count(*) FROM synthetic_pr192_print_jobs").fetchone()[0]

    def update(self, sql, args=()):
        with connect() as conn:
            conn.execute(sql, args)

    def test_first_scan_queues_without_worker_selecting_order(self):
        response = self.scan()
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["queued_new"], 1)
        self.assertFalse(response.json["physical_printing"])
        self.assertEqual(self.job_count(), 1)
        self.assertEqual(read_synthetic_print_jobs(connect, printer_id="p1")[0][5], "QUEUED")

    def test_repeat_scan_does_not_duplicate(self):
        self.scan()
        second = self.scan()
        self.assertEqual(second.json["queued_new"], 0)
        self.assertEqual(second.json["already_queued"], 1)
        self.assertEqual(self.job_count(), 1)

    def test_new_arrival_added_at_second_scan(self):
        self.assertEqual(self.scan().json["queued_new"], 1)
        self.update("""INSERT INTO wb_fbs_orders VALUES
            ('synthetic','o2','confirm','pending')""")
        self.update("INSERT INTO wb_fbs_picking_queue VALUES ('synthetic','o2')")
        self.update("INSERT INTO wb_fbs_order_skus VALUES ('synthetic','o2','SKU1')")
        self.update("""INSERT INTO synthetic_pr192_print_stickers VALUES
            ('synthetic','o2','TEST-ONLY-ORIGINAL-STICKER-2')""")
        result = self.scan()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["queued_new"], 1)
        self.assertEqual(result.json["already_queued"], 1)
        self.assertEqual(self.job_count(), 2)

    def test_missing_original_sticker_rejected(self):
        self.update("DELETE FROM synthetic_pr192_print_stickers")
        result=self.scan()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["queued_new"], 0)
        self.assertEqual(result.json["requires_review"], 1)
        self.assertEqual(self.job_count(), 0)

    def test_wrong_line_denied(self):
        self.update("UPDATE replenishment_locations SET label='В-03-1'")
        result=self.scan()
        self.assertEqual(result.json["queued_new"], 0)
        self.assertEqual(result.json["requires_review"], 1)

    def test_missing_sku_denied(self):
        self.update("DELETE FROM wb_fbs_order_skus")
        self.assertEqual(self.scan().json["queued_new"], 0)
        self.assertEqual(self.job_count(), 0)

    def test_cancelled_order_not_queued(self):
        self.update("UPDATE wb_fbs_orders SET wb_status='cancel'")
        self.assertEqual(self.scan().json["queued_new"], 0)

    def test_qr_rotation_denied(self):
        other, _ = create_printer("p1", "Линии А-Б", ["А", "Б"])
        self.update("UPDATE grit_line_printers SET token_digest=%s",
                    (other.token_digest,))
        self.assertEqual(self.scan().status_code, 403)
        self.assertEqual(self.job_count(), 0)

    def test_printer_disabled_denied(self):
        self.update("UPDATE grit_line_printers SET enabled=0")
        self.assertEqual(self.scan().status_code, 403)

    def test_employee_disabled_denied(self):
        self.update("UPDATE employees SET active=0 WHERE id='e1'")
        self.assertEqual(self.scan().status_code, 403)
        self.assertEqual(self.job_count(), 0)

    def test_shift_closed_denied(self):
        self.update("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
        self.assertEqual(self.scan().status_code, 403)

    def test_picking_closed_denied(self):
        self.update("UPDATE activities SET ended_at='closed' WHERE shift_id='s1'")
        self.assertEqual(self.scan().status_code, 403)

    def test_manager_cannot_scan_as_worker(self):
        self.assertEqual(self.scan(self.manager).status_code, 401)

    def test_csrf_required(self):
        self.assertEqual(self.scan(csrf="wrong").status_code, 403)
        self.assertEqual(self.job_count(), 0)

    def test_spoofed_order_lines_or_identity_rejected(self):
        self.assertEqual(self.scan(order_id="o1").status_code, 400)
        self.assertEqual(self.scan(lines=["В"]).status_code, 400)
        self.assertEqual(self.scan(employee_id="e2").status_code, 400)
        self.assertEqual(self.job_count(), 0)

    def test_bad_qr_denied(self):
        self.assertEqual(self.scan(qr="GRIT-PRINTER:1:p1:wrong").status_code, 403)

    def test_same_scan_from_eight_terminals_is_idempotent(self):
        def task(_):
            return scan_synthetic_printer_qr(
                connect, qr_payload=self.qr, employee_id="e1", shift_id="s1")
        with ThreadPoolExecutor(max_workers=8) as executor:
            result = list(executor.map(task, range(8)))
        self.assertEqual(sum(r.added for r in result), 1)
        self.assertEqual(self.job_count(), 1)

    def test_unbound_session_denied(self):
        with self.worker.session_transaction() as s:
            s.pop("attendance_verified")
        self.assertEqual(self.scan().status_code, 401)

    def test_no_live_registration_when_flag_off(self):
        value=os.environ.pop("GRIT_SYNTHETIC_PG_TEST",None)
        try:
            with self.assertRaisesRegex(RuntimeError, "disabled"):
                make_synthetic_autoqueue_blueprint(connect,authenticated_employee)
        finally:
            if value is not None:
                os.environ["GRIT_SYNTHETIC_PG_TEST"]=value


if __name__ == "__main__":
    unittest.main()
