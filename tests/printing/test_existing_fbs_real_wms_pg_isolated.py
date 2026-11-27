"""PR209: actual existing /fbs HTML + actual WMS badge/shift + isolated QR enqueue.

This intentionally registers the existing WMS blueprint ONLY in a disposable
Flask test app. Production app.py is never imported, the PostgreSQL namespace
must be synthetic, and NO USB / WB network / physical printing takes place.
"""
import importlib.util
import os
from pathlib import Path
import unittest

import psycopg

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("PR209 isolated synthetic PostgreSQL gate required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("PR209 refuses non-synthetic PostgreSQL DSN")

source = Path(__file__).resolve().parent / "test_qr_real_wms_auth.py"
spec = importlib.util.spec_from_file_location("pr209_real_wms_fixture", source)
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def pg():
    return psycopg.connect(DSN)


class ActualFbsPrinterQrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.RealWMSAutoqueueTests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture.RealWMSAutoqueueTests.doClassCleanups()

    def setUp(self):
        self.case = fixture.RealWMSAutoqueueTests(
            "test_real_wms_authorizes_qr_and_autoqueue")
        self.case.setUp()
        self.app = self.case.app
        self.app.register_blueprint(self.case.wms.wms_bp)
        self.worker = self.case.worker
        self.manager = self.case.manager

    def test_existing_worker_fbs_screen_contains_single_scan_handler(self):
        response = self.worker.get("/fbs")
        self.assertEqual(response.status_code, 200, response.data[:200])
        page = response.get_data(as_text=True)
        self.assertIn("async function handleScan(code)", page)
        self.assertIn("if(code.startsWith('GRIT-PRINTER:'))await acceptPrinterQr(code)", page)
        self.assertIn("else if(pendingProduct)await acceptWb(code)", page)
        self.assertIn("else await acceptProduct(code)", page)
        self.assertIn('id="barcode"', page)
        self.assertNotIn("/demo/worker-qr", page)

    def test_regular_wms_login_still_authorizes_current_shift(self):
        me = self.worker.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.json)
        self.assertEqual(me.json["employee"]["id"], "e1")
        shift = self.worker.get("/api/shift/current")
        self.assertEqual(shift.status_code, 200, shift.json)
        self.assertTrue(shift.json["active"])

    def test_real_fbs_session_scan_enqueues_without_touching_wms_order_counts(self):
        prior = self.case.wms.query_one("SELECT COUNT(*) FROM scans")[0]
        token = self.worker.get("/synthetic/pr192/print-queue/session")
        self.assertEqual(token.status_code, 200, token.json)
        queued = self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr": self.case.qr, "csrf": token.json["csrf"]})
        self.assertEqual(queued.status_code, 200, queued.json)
        self.assertEqual(queued.json["queued_new"], 1)
        self.assertFalse(queued.json["physical_printing"])
        again = self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr": self.case.qr, "csrf": token.json["csrf"]})
        self.assertEqual(again.status_code, 200, again.json)
        self.assertEqual(again.json["already_queued"], 1)
        self.assertEqual(self.case.wms.query_one("SELECT COUNT(*) FROM scans")[0], prior)
        with pg() as conn:
            rows = conn.execute("""SELECT order_id, state, printer_id
                FROM synthetic_pr192_print_jobs""").fetchall()
            self.assertEqual(rows, [("o1", "QUEUED", "p1")])

    def test_manager_cannot_scan_on_fbs_as_worker(self):
        response = self.manager.get("/fbs")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers.get("Location", ""))
        self.assertEqual(self.manager.get(
            "/synthetic/pr192/print-queue/session").status_code, 401)

    def test_disabled_printer_fails_closed_and_no_wms_picking_scan(self):
        with pg() as conn:
            conn.execute("UPDATE grit_line_printers SET enabled=0 WHERE printer_id='p1'")
        token = self.worker.get("/synthetic/pr192/print-queue/session")
        response = self.worker.post("/synthetic/pr192/print-queue/scan",
            json={"qr": self.case.qr, "csrf": token.json["csrf"]})
        self.assertEqual(response.status_code, 403)
        with pg() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM synthetic_pr192_print_jobs").fetchone()[0], 0)
        self.assertEqual(self.case.wms.query_one("SELECT COUNT(*) FROM scans")[0], 0)

    def test_closed_shift_denies_both_real_fbs_and_printer_qr(self):
        with self.case.wms.db_connect() as conn:
            conn.execute("UPDATE shifts SET ended_at='2026-11-27T10:00:00' WHERE id='s1'")
            conn.commit()
        response = self.worker.get("/fbs")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.worker.get(
            "/synthetic/pr192/print-queue/session").status_code, 401)


if __name__ == "__main__":
    unittest.main()
