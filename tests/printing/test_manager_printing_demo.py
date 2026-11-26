"""Manager-only queue status and one-job virtual ACK, isolated PG17 + WMS auth.

Test class reuses real wms.get_session_employee() and synthetic worker fixture.
No production routes, real WB accounts or physical Xprinter interactions.
"""
import importlib.util
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import psycopg
from flask import session

from services.printing.manager_ui_synthetic import (
    make_synthetic_printing_manager_blueprint,
)
from services.printing.virtual_agent import initialize_virtual_sink

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PG test flag required")
DSN=os.getenv("PR192_SYNTHETIC_DATABASE_URL","")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing non-synthetic PostgreSQL DSN")

spec=importlib.util.spec_from_file_location(
    "pr192_manager_real_wms_fixture",
    Path(__file__).resolve().parent / "test_qr_real_wms_auth.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


def pg():
    return psycopg.connect(DSN)


class VirtualManagerUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.RealWMSAutoqueueTests.setUpClass()
        with pg() as conn:
            initialize_virtual_sink(conn)

    @classmethod
    def tearDownClass(cls):
        fixture.RealWMSAutoqueueTests.doClassCleanups()

    def setUp(self):
        with pg() as conn:
            conn.execute("DELETE FROM synthetic_pr192_virtual_receipts")
        self.wms_case=fixture.RealWMSAutoqueueTests(
            "test_real_wms_authorizes_qr_and_autoqueue")
        self.wms_case.setUp()
        self.app=self.wms_case.app
        self.worker=self.wms_case.worker
        self.manager=self.wms_case.manager
        with self.manager.session_transaction() as s:
            s["pr192_manager_id"]="synthetic-manager-id"
            s["pr192_manager_csrf"]="synthetic-manager-csrf-123456"
        def authorized_manager():
            return (session.get("manager_web") is True and
                    bool(session.get("pr192_manager_id")) and
                    self.wms_case.wms.manager_access())
        self.app.register_blueprint(
            make_synthetic_printing_manager_blueprint(pg,authorized_manager))

    def queue(self):
        response=self.wms_case.scan()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["queued_new"],1)
        return response

    def dispatch(self, client=None, **changes):
        data={"printer_id":"p1","csrf":"synthetic-manager-csrf-123456"}
        data.update(changes)
        return (client or self.manager).post(
            "/demo/printing/virtual-deliver",json=data)

    def state(self):
        return self.manager.get("/demo/printing/state")

    def test_manager_can_open_dashboard(self):
        response=self.manager.get("/demo/printing")
        self.assertEqual(response.status_code,200)
        data=response.get_data(as_text=True)
        self.assertIn("Очередь виртуальной печати",data)
        self.assertIn("id=\"jobs\"",data)
        self.assertIn("/synthetic/pr192/assets/printing_queue.js",data)
        self.assertEqual(response.headers.get("Cache-Control"),"no-store")

    def test_worker_cannot_open_dashboard(self):
        self.assertEqual(self.worker.get("/demo/printing").status_code,403)

    def test_worker_cannot_fetch_manager_script(self):
        self.assertEqual(self.worker.get(
            "/synthetic/pr192/assets/printing_queue.js").status_code,403)

    def test_manager_can_fetch_fsd_script(self):
        response=self.manager.get(
            "/synthetic/pr192/assets/printing_queue.js")
        self.assertEqual(response.status_code,200)
        self.assertIn("printing/state",response.get_data(as_text=True))

    def test_state_displays_queued_job_without_sticker(self):
        self.queue()
        response=self.state()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["jobs"][0]["state"],"QUEUED")
        self.assertEqual(response.json["jobs"][0]["order_id"],"o1")
        self.assertFalse(response.json["physical_printing"])
        self.assertNotIn("original_sticker",response.get_data(as_text=True))
        self.assertNotIn("SYNTH-WB-STICKER-1",response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"),"no-store")

    def test_worker_cannot_read_state(self):
        self.assertEqual(self.worker.get("/demo/printing/state").status_code,403)

    def test_virtual_one_job_submission_and_audit(self):
        self.queue()
        response=self.dispatch()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["state"],"SUBMITTED")
        self.assertFalse(response.json["physical_printing"])
        snapshot=self.state().json
        self.assertEqual(snapshot["jobs"][0]["state"],"SUBMITTED")
        self.assertEqual([event["to"] for event in snapshot["events"][:3]],
                         ["SUBMITTED","SENDING","QUEUED"])
        with pg() as conn:
            count=conn.execute(
                "SELECT count(*) FROM synthetic_pr192_virtual_receipts").fetchone()[0]
        self.assertEqual(count,1)

    def test_repeated_manager_dispatch_does_not_redeliver(self):
        self.queue()
        self.assertEqual(self.dispatch().json["state"],"SUBMITTED")
        result=self.dispatch()
        self.assertEqual(result.json["state"],"EMPTY")
        with pg() as conn:
            count=conn.execute(
                "SELECT count(*) FROM synthetic_pr192_virtual_receipts").fetchone()[0]
        self.assertEqual(count,1)

    def test_manager_cannot_select_job_id(self):
        self.queue()
        self.assertEqual(self.dispatch(job_id="o1").status_code,400)
        self.assertEqual(self.state().json["jobs"][0]["state"],"QUEUED")

    def test_manager_cannot_set_delivery_mode(self):
        self.queue()
        self.assertEqual(self.dispatch(mode="PRINTED").status_code,400)
        self.assertEqual(self.state().json["jobs"][0]["state"],"QUEUED")

    def test_missing_or_bad_csrf_denied(self):
        self.queue()
        self.assertEqual(self.dispatch(csrf="wrong").status_code,403)
        self.assertEqual(self.state().json["jobs"][0]["state"],"QUEUED")

    def test_worker_cannot_call_virtual_agent(self):
        self.queue()
        self.assertEqual(self.dispatch(self.worker).status_code,403)
        self.assertEqual(self.state().json["jobs"][0]["state"],"QUEUED")

    def test_nonexistent_printer_cannot_dispatch(self):
        self.assertEqual(self.dispatch(printer_id="nonexistent").status_code,404)

    def test_disabled_printer_job_is_not_delivered(self):
        self.queue()
        with pg() as conn:
            conn.execute("UPDATE grit_line_printers SET enabled=0")
        response=self.dispatch()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["state"],"CANCELLED")
        with pg() as conn:
            count=conn.execute(
                "SELECT count(*) FROM synthetic_pr192_virtual_receipts").fetchone()[0]
        self.assertEqual(count,0)

    def test_manager_factory_requires_isolated_pg_gate(self):
        with patch.dict(os.environ,{"GRIT_SYNTHETIC_PG_TEST":"0"}):
            with self.assertRaisesRegex(RuntimeError,"disabled"):
                make_synthetic_printing_manager_blueprint(
                    pg,lambda:True)


if __name__=="__main__":
    unittest.main()
