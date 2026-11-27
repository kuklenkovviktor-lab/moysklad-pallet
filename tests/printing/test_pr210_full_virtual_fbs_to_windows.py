"""One-command synthetic acceptance: existing GRIT worker auth → printer QR →
PostgreSQL17 line-routing → auto USB claim → actual Windows agent code with
FAKE spooler → audit, idempotency and no manual manager login/QR scan/hardware.

Strictly disposable PG17 from verify_line_printer_print_queue_pg_isolated.sh.
No production, no Windows driver, no live marketplace orders.
"""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PostgreSQL required")

here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "pr210_usb_pg_fixture", here / "test_usb_bridge_synthetic.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
from services.printing import grit_windows_usb_agent as windows_agent
from services.printing import grit_usb_print_pilot

FAKE_TOKEN = fixture.TOKEN


class VirtualFbsToWindowsAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.UsbBridgePG17Tests.setUpClass()

    @classmethod
    def tearDownClass(cls):
        fixture.UsbBridgePG17Tests.doClassCleanups()

    def setUp(self):
        self.case = fixture.UsbBridgePG17Tests("test_manager_can_read_empty_bridge_status")
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.wms = self.case.case.wms_case.wms
        self.worker = self.case.worker

    def server_api(self, method, path, body=None):
        response = self.case.device(method, path, body)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_no_manager_no_scanner_virtual_full_path(self):
        from services.printing.usb_test_preparation import stand_identity
        with tempfile.TemporaryDirectory() as state_dir, mock.patch.dict(os.environ, {
            "GRIT_PR210_AUTO_USB_SYNTHETIC": "YES_EXPLICIT",
            "GRIT_PR210_USB_PRINTER_ID": "p1",
            "GRIT_PR192_USB_AGENT_TOKEN": FAKE_TOKEN,
        }):
            # Existing /fbs page, existing badge-bound WMS worker session.
            self.case.app.register_blueprint(self.wms.wms_bp)
            fbs = self.worker.get("/fbs")
            self.assertEqual(fbs.status_code, 200, fbs.data[:200])
            self.assertIn("async function handleScan(code)", fbs.get_data(as_text=True))
            # No test manager login or manually approved USB ticket.
            with fixture.pg() as conn:
                stand = stand_identity(conn)
                existing = conn.execute(
                    "SELECT COUNT(*) FROM synthetic_pr192_usb_approvals").fetchone()[0]
            self.assertEqual(existing, 0)
            session_result = self.worker.get("/synthetic/pr192/print-queue/session")
            self.assertEqual(session_result.status_code, 200, session_result.json)
            scan = self.worker.post("/synthetic/pr192/print-queue/scan", json={
                "qr": self.case.case.wms_case.qr,
                "csrf": session_result.json["csrf"],
            })
            self.assertEqual(scan.status_code, 200, scan.json)
            self.assertEqual(scan.json["auto_usb_approved"], 1)
            self.assertEqual(scan.json["queued_new"], 0)

            # Run actual USB-agent claim/journal/report implementation against
            # Flask in-memory HTTP + a fake Windows spooler. No device access.
            fake = grit_usb_print_pilot.TEST_PRINTER
            spooler_spec = importlib.util.spec_from_file_location(
                "pr210_fake_windows_spooler",here/"test_grit_usb_print_pilot.py")
            spooler_module = importlib.util.module_from_spec(spooler_spec)
            spooler_spec.loader.exec_module(spooler_module)
            spooler = spooler_module.FakeSpooler()
            result, journal = windows_agent.run_once(
                self.server_api, spooler, state_dir=state_dir,
                automatic_stand=stand)
            self.assertEqual(result, "SUBMITTED_TO_WINDOWS")
            self.assertIsNotNone(journal)
            self.assertEqual(spooler.events.count("write"), 1)
            self.assertEqual(spooler.last_opened, fake)
            self.assertIn(b"NOT WB", spooler.payload)
            self.assertEqual(spooler.payload.count(b"PRINT 1,1"), 1)

            # Repeated QR + agent invocation must never spool twice.
            rescan = self.worker.post("/synthetic/pr192/print-queue/scan",json={
                "qr":self.case.case.wms_case.qr,
                "csrf":session_result.json["csrf"],
            })
            self.assertEqual(rescan.status_code, 200, rescan.json)
            self.assertEqual(rescan.json["auto_usb_approved"], 0)
            self.assertEqual(rescan.json["already_queued"], 1)
            state, second = windows_agent.run_once(
                self.server_api, spooler, state_dir=state_dir,
                automatic_stand=stand)
            self.assertEqual((state, second), ("EMPTY", None))
            self.assertEqual(spooler.events.count("write"),1)
            with fixture.pg() as conn:
                final = conn.execute("""SELECT j.state,j.delivery_transport,
                    a.phase,a.result FROM synthetic_pr192_print_jobs j
                    JOIN synthetic_pr192_usb_approvals a USING (job_id)
                    WHERE j.order_id='o1'""").fetchone()
                events = conn.execute("""SELECT event FROM synthetic_pr192_print_events
                    WHERE job_id=(SELECT job_id FROM synthetic_pr192_print_jobs
                                  WHERE order_id='o1') ORDER BY event_id""").fetchall()
            self.assertEqual(final,("SUBMITTED","USB","REPORTED","SUBMITTED_TO_WINDOWS"))
            self.assertIn(("AUTO_USB_APPROVAL",), events)
            self.assertEqual(self.wms.query_one("SELECT count(*) FROM scans")[0],0)

    def test_no_automatic_mode_does_not_approve_or_print(self):
        worker_token = self.worker.get("/synthetic/pr192/print-queue/session")
        response = self.worker.post("/synthetic/pr192/print-queue/scan",json={
            "qr":self.case.case.wms_case.qr,
            "csrf":worker_token.json["csrf"],
        })
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["auto_usb_approved"],0)
        with fixture.pg() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM synthetic_pr192_usb_approvals").fetchone()[0],0)


if __name__=="__main__":
    unittest.main()
