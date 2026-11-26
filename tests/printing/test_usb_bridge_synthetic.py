"""Isolated PostgreSQL17 + real manager session USB bridge tests; NO devices.

The one-shot USB bridge handles only synthetic rows, manager approval,
device token, destructive claim, no-retry status, and no original stickers.
"""
import importlib.util
import os
from pathlib import Path
from unittest import TestCase, main, mock
from flask import session

from services.printing.usb_bridge_synthetic import (
    initialize_synthetic_usb_bridge,
    make_synthetic_usb_bridge_blueprint,
)
from services.printing.virtual_agent import initialize_virtual_sink

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Synthetic PostgreSQL gate required")
DSN=os.getenv("PR192_SYNTHETIC_DATABASE_URL","")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Disallowed non-test database")

import psycopg


def pg():
    return psycopg.connect(DSN)


spec=importlib.util.spec_from_file_location(
    "isolated_manager_fixture_for_usb_bridge",
    Path(__file__).resolve().parent / "test_manager_printing_demo.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)

TOKEN="isolated-one-shot-usb-bridge-token-not-real-1234567890"


class UsbBridgePG17Tests(TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.VirtualManagerUITests.setUpClass()
        with pg() as conn:
            initialize_synthetic_usb_bridge(conn)

    @classmethod
    def tearDownClass(cls):
        fixture.VirtualManagerUITests.doClassCleanups()

    def setUp(self):
        with pg() as conn:
            conn.execute("DELETE FROM synthetic_pr192_usb_approvals")
        self.case=fixture.VirtualManagerUITests("test_manager_can_open_dashboard")
        self.case.setUp()
        self.app=self.case.app
        self.manager=self.case.manager
        self.worker=self.case.worker
        self.agent=self.app.test_client()
        def authorized():
            return (session.get("manager_web") is True and
                    bool(session.get("pr192_manager_id")) and
                    self.case.wms_case.wms.manager_access())
        with mock.patch.dict(os.environ,{"GRIT_PR192_USB_AGENT_TOKEN":TOKEN}):
            self.app.register_blueprint(
                make_synthetic_usb_bridge_blueprint(pg,authorized))
        self.case.queue()

    def tearDown(self):
        with pg() as conn:
            conn.execute("DELETE FROM synthetic_pr192_usb_approvals")

    def approve(self, client=None, **changes):
        payload={"printer_id":"p1","csrf":"synthetic-manager-csrf-123456"}
        payload.update(changes)
        return (client or self.manager).post(
            "/demo/printing/usb/approve",json=payload)

    def device(self,method,path,body=None,token=TOKEN):
        opts={"headers":{"X-GRIT-USB-Agent-Token":token}}
        if method=="GET":
            return self.agent.get(path,**opts)
        return self.agent.post(path,json={} if body is None else body,**opts)

    def claim(self,body=None,token=TOKEN):
        return self.device("POST","/synthetic/pr192/usb-agent/claim",body,token)

    def ping(self,token=TOKEN):
        return self.device("GET","/synthetic/pr192/usb-agent/ping",token=token)

    def report(self,ticket,result="SUBMITTED_TO_WINDOWS",spool_id=111,token=TOKEN):
        return self.device("POST","/synthetic/pr192/usb-agent/report",
                           {"ticket_id":ticket,"result":result,
                            "spool_job_id":spool_id},token=token)

    def state(self):
        return self.manager.get("/demo/printing/state").json

    def test_manager_can_read_empty_bridge_status(self):
        response=self.manager.get("/demo/printing/usb/state")
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json["synthetic"])
        self.assertFalse(response.json["physical_printing"])
        self.assertEqual(response.json["approvals"],[])
        self.assertEqual(response.json["windows_printer"],"ONIX-TEST-XP460B")

    def test_worker_denied_manager_status_and_approval(self):
        self.assertEqual(self.worker.get("/demo/printing/usb/state").status_code,403)
        self.assertEqual(self.approve(self.worker).status_code,403)
        self.assertEqual(self.state()["jobs"][0]["state"],"QUEUED")

    def test_bad_csrf_and_body_denied(self):
        self.assertEqual(self.approve(csrf="no").status_code,403)
        self.assertEqual(self.approve(job_id="o1").status_code,400)
        self.assertEqual(self.approve(mode="PRINTED").status_code,400)
        self.assertEqual(self.state()["jobs"][0]["state"],"QUEUED")

    def test_device_token_required_for_ping_claim_report(self):
        self.assertEqual(self.ping(token="wrong").status_code,403)
        self.assertEqual(self.claim(token="wrong").status_code,403)
        self.assertEqual(self.report("a"*32,token="wrong").status_code,403)
        self.assertEqual(self.state()["jobs"][0]["state"],"QUEUED")

    def test_manager_approval_does_not_print_or_claim(self):
        r=self.approve()
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(r.json["state"],"APPROVED")
        self.assertFalse(r.json["physical_printing"])
        self.assertEqual(self.state()["jobs"][0]["state"],"QUEUED")
        self.assertEqual(self.ping().json["waiting"],1)

    def test_approving_twice_is_blocked(self):
        self.assertEqual(self.approve().json["state"],"APPROVED")
        self.assertEqual(self.approve().status_code,409)
        self.assertEqual(self.ping().json["waiting"],1)

    def test_no_approval_returns_empty_to_device(self):
        response=self.claim()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json["state"],"EMPTY")
        self.assertEqual(self.state()["jobs"][0]["state"],"QUEUED")

    def test_claim_rejects_injected_body(self):
        self.assertEqual(self.approve().json["state"],"APPROVED")
        self.assertEqual(self.claim(body={"printer_id":"p1"}).status_code,400)
        self.assertEqual(self.ping().json["waiting"],1)

    def test_claim_once_then_spooler_ack_is_submitted_not_printed(self):
        self.assertEqual(self.approve().json["state"],"APPROVED")
        claim=self.claim()
        self.assertEqual(claim.status_code,200,claim.json)
        self.assertEqual(claim.json["state"],"CLAIMED")
        self.assertEqual(claim.json["windows_printer"],"ONIX-TEST-XP460B")
        self.assertRegex(claim.json["ticket_id"],r"^[0-9a-f]{32}$")
        self.assertNotIn("original_sticker",claim.get_data(as_text=True))
        self.assertNotIn("SYNTH-WB-STICKER-1",claim.get_data(as_text=True))
        self.assertEqual(self.state()["jobs"][0]["state"],"SENDING")
        self.assertEqual(self.claim().json["state"],"EMPTY")
        reported=self.report(claim.json["ticket_id"])
        self.assertEqual(reported.status_code,200,reported.json)
        self.assertEqual(reported.json["state"],"SUBMITTED")
        self.assertFalse(reported.json["physical_printing"])
        self.assertEqual(self.state()["jobs"][0]["state"],"SUBMITTED")
        self.assertEqual(self.report(claim.json["ticket_id"]).status_code,409)
        with pg() as conn:
            record=conn.execute("""SELECT phase,result,spool_job_id
                FROM synthetic_pr192_usb_approvals""").fetchone()
        self.assertEqual(record,("REPORTED","SUBMITTED_TO_WINDOWS",111))

    def test_claim_unknown_never_automatic_requeues(self):
        self.approve()
        ticket=self.claim().json["ticket_id"]
        r=self.report(ticket,result="UNKNOWN",spool_id=None)
        self.assertEqual(r.status_code,200,r.json)
        self.assertEqual(r.json["state"],"UNKNOWN")
        self.assertEqual(self.state()["jobs"][0]["state"],"UNKNOWN")
        self.assertEqual(self.claim().json["state"],"EMPTY")
        self.assertEqual(self.approve().json["state"],"EMPTY")

    def test_claim_crash_stays_sending_and_blocks_next_approval(self):
        self.approve()
        self.claim()
        self.assertEqual(self.state()["jobs"][0]["state"],"SENDING")
        self.assertEqual(self.approve().status_code,409)
        self.assertEqual(self.claim().json["state"],"EMPTY")

    def test_disabled_printer_cancelled_before_claim(self):
        self.approve()
        with pg() as conn:
            conn.execute("UPDATE grit_line_printers SET enabled=0 WHERE printer_id='p1'")
        r=self.claim()
        self.assertEqual(r.json["state"],"REJECTED")
        self.assertEqual(self.state()["jobs"][0]["state"],"CANCELLED")
        self.assertEqual(self.ping().json["waiting"],0)

    def test_stale_order_cancelled_before_manager_approval(self):
        with pg() as conn:
            conn.execute("""UPDATE wb_fbs_orders SET wb_status='cancel'
                WHERE account_id='synthetic' AND order_id='o1'""")
        r=self.approve()
        self.assertEqual(r.json["state"],"CANCELLED")
        self.assertEqual(self.state()["jobs"][0]["state"],"CANCELLED")
        self.assertEqual(self.ping().json["waiting"],0)

    def test_invalid_spool_report_does_not_advance_job(self):
        self.approve()
        ticket=self.claim().json["ticket_id"]
        self.assertEqual(self.report(ticket,spool_id=0).status_code,400)
        self.assertEqual(self.report(ticket,result="UNKNOWN",spool_id=111).status_code,400)
        self.assertEqual(self.report(ticket,result="PRINTED").status_code,400)
        self.assertEqual(self.state()["jobs"][0]["state"],"SENDING")

    def test_factory_refuses_without_gate_or_secret(self):
        with mock.patch.dict(os.environ,{"GRIT_SYNTHETIC_PG_TEST":"0"}):
            with self.assertRaises(RuntimeError):
                make_synthetic_usb_bridge_blueprint(pg,lambda: True)
        with mock.patch.dict(os.environ,{"GRIT_PR192_USB_AGENT_TOKEN":"short"}):
            with self.assertRaises(RuntimeError):
                make_synthetic_usb_bridge_blueprint(pg,lambda: True)


if __name__=="__main__":
    main()
