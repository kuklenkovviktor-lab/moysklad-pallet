"""Isolated QR worker UI + existing WMS auth + PG17 HTTP regression.

Tests ONLY the new /demo/worker-qr route in a disposable WMS/PG fixture;
the actual production /fbs page is not changed in this PR.
"""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import psycopg

from services.printing.worker_ui_synthetic import make_synthetic_worker_scanner_blueprint

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PG test flag required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing non-synthetic PG")

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "pr192_existing_auth_fixture",
    Path(__file__).resolve().parent / "test_qr_real_wms_auth.py")
fixture_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)


def connect():
    return psycopg.connect(DSN)


class WorkerScannerDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.RealWMSAutoqueueTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture_module.RealWMSAutoqueueTests.doClassCleanups()

    def setUp(self):
        self.fixture = fixture_module.RealWMSAutoqueueTests(
            "test_real_wms_authorizes_qr_and_autoqueue")
        self.fixture.setUp()
        self.qr = self.fixture.qr
        self.app = self.fixture.app
        self.app.register_blueprint(
            make_synthetic_worker_scanner_blueprint(
                connect, self.fixture.wms.get_session_employee))
        self.worker = self.fixture.worker
        self.manager = self.fixture.manager

    def test_authenticated_worker_uses_fsd_scanner_page(self):
        r = self.worker.get("/demo/worker-qr")
        self.assertEqual(r.status_code, 200)
        h = r.get_data(as_text=True)
        self.assertIn('id="barcode"',h)
        self.assertIn('id="result"',h)
        self.assertIn("/synthetic/pr192/assets/printer_scan.js",h)
        self.assertIn("СИНТЕТИЧЕСКОЕ ДЕМО",h)
        self.assertIn("Товарные ШК",h)
        self.assertNotIn('id="order_id"',h)
        self.assertNotIn("onclick=\"reserve",h)
        self.assertEqual(r.headers.get("Cache-Control"),"no-store")

    def test_manager_cannot_open_worker_scanner(self):
        self.assertEqual(self.manager.get("/demo/worker-qr").status_code,401)

    def test_unverified_session_cannot_open_worker_scanner(self):
        with self.worker.session_transaction() as s:
            s.pop("attendance_verified",None)
        self.assertEqual(self.worker.get("/demo/worker-qr").status_code,401)

    def test_closed_shift_blocks_worker_page(self):
        with self.fixture.wms.db_connect() as conn:
            conn.execute("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
            conn.commit()
        self.assertEqual(self.worker.get("/demo/worker-qr").status_code,401)

    def test_fsd_scanner_script_served_without_caching(self):
        r=self.worker.get("/synthetic/pr192/assets/printer_scan.js")
        self.assertEqual(r.status_code,200)
        content=r.get_data(as_text=True)
        self.assertIn("window",content)
        self.assertIn("GRITPrinterScan",content)
        self.assertIn("GRIT-PRINTER:",content)
        self.assertIn('credentials: "same-origin"',content)
        self.assertIn("includes(event.target.tagName)",content)
        self.assertNotIn("/api/fbs/scan",content)
        self.assertEqual(r.headers.get("Cache-Control"),"no-store")

    def test_unverified_user_cannot_fetch_script(self):
        self.assertEqual(self.manager.get(
            "/synthetic/pr192/assets/printer_scan.js").status_code,401)

    def test_get_session_issues_csrf_to_real_wms_worker(self):
        r=self.worker.get("/synthetic/pr192/print-queue/session")
        self.assertEqual(r.status_code,200,r.json)
        self.assertTrue(r.json["synthetic"])
        self.assertFalse(r.json["physical_printing"])
        self.assertGreaterEqual(len(r.json["csrf"]),16)
        self.assertEqual(r.headers.get("Cache-Control"),"no-store")
        again=self.worker.get("/synthetic/pr192/print-queue/session")
        self.assertEqual(again.json["csrf"],r.json["csrf"])

    def test_manager_cannot_obtain_scanner_csrf(self):
        self.assertEqual(self.manager.get(
            "/synthetic/pr192/print-queue/session").status_code,401)

    def test_worker_browser_can_scan_qr_without_order_id(self):
        r=self.worker.get("/synthetic/pr192/print-queue/session")
        csrf=r.json["csrf"]
        queued=self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr":self.qr,"csrf":csrf})
        self.assertEqual(queued.status_code,200,queued.json)
        self.assertEqual(queued.json["queued_new"],1)
        self.assertFalse(queued.json["physical_printing"])
        again=self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr":self.qr,"csrf":csrf})
        self.assertEqual(again.json["already_queued"],1)
        with connect() as conn:
            self.assertEqual(conn.execute("""SELECT count(*)
                FROM synthetic_pr192_print_jobs""").fetchone()[0],1)

    def test_scanner_rejects_unknown_qr_not_a_product_barcode(self):
        csrf=self.worker.get(
            "/synthetic/pr192/print-queue/session").json["csrf"]
        result=self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr":"SKU1","csrf":csrf})
        self.assertEqual(result.status_code,403)
        with connect() as conn:
            self.assertEqual(conn.execute("""SELECT count(*)
                FROM synthetic_pr192_print_jobs""").fetchone()[0],0)

    def test_csrf_required_for_request(self):
        self.assertEqual(self.fixture.scan(csrf="wrong").status_code,403)

    def test_no_production_fbs_page_or_routes_registered(self):
        self.assertEqual(self.worker.get("/fbs").status_code,404)
        self.assertIsNone(self.app.view_functions.get("fbs_page"))

    def test_synthetic_ui_requires_pg_test_gate(self):
        with patch.dict(os.environ,{"GRIT_SYNTHETIC_PG_TEST":"0"}):
            with self.assertRaisesRegex(RuntimeError,"disabled"):
                make_synthetic_worker_scanner_blueprint(
                    connect,self.fixture.wms.get_session_employee)


if __name__ == "__main__":
    unittest.main()
