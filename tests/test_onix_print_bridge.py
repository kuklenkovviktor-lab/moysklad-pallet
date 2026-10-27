"""Tests for opt-in ONIX HTTPS print bridge. No physical printer required."""
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET","test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET","test-terminal")
os.environ.setdefault("SECRET_KEY","test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN","test-only")
os.environ["WMS_REQUIRE_POSTGRES"]="0"
os.environ.pop("DATABASE_URL",None)
_TMP=tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"]=os.path.join(_TMP.name,"onix-print-test.db")

import app
import onix_print_agent
from wms import query_all, query_one


TEST_SECRET="test-agent-secret-"+"x"*40
HEADERS={"Authorization":"Bearer "+TEST_SECRET}


class OnixPrintBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app._WMS_READY.set()

    def setUp(self):
        self.client=app.app.test_client()
        with self.client.session_transaction() as s:
            s["manager_web"]=True
        self.client.get("/manager/onix-print-test")
        with self.client.session_transaction() as s:
            self.csrf=s["onix_print_csrf"]

    def _enqueue(self):
        return self.client.post("/manager/onix-print-test",data={
            "csrf":self.csrf,"confirmation":"one-test-label"})

    def test_bridge_disabled_without_secret(self):
        with patch.dict(os.environ,{"ONIX_PRINT_AGENT_TOKEN":""}):
            self.assertEqual(self._enqueue().status_code,503)
            self.assertEqual(self.client.get("/api/print-agent/test/next",
                            headers=HEADERS).status_code,401)

    def test_auth_and_csrf(self):
        with patch.dict(os.environ,{"ONIX_PRINT_AGENT_TOKEN":TEST_SECRET}):
            stranger=app.app.test_client()
            self.assertNotEqual(stranger.post("/manager/onix-print-test",data={
                "csrf":self.csrf,"confirmation":"one-test-label"}).status_code,303)
            self.assertEqual(self.client.post("/manager/onix-print-test",
                data={"confirmation":"one-test-label"}).status_code,403)
            self.assertEqual(self.client.get("/api/print-agent/test/next").status_code,401)
            self.assertEqual(self.client.get("/api/print-agent/test/next",
                headers={"Authorization":"Bearer bad"}).status_code,401)

    def test_exactly_once_claim_and_ack(self):
        with patch.dict(os.environ,{"ONIX_PRINT_AGENT_TOKEN":TEST_SECRET}):
            r=self._enqueue()
            self.assertEqual(r.status_code,303)
            self.assertEqual(self._enqueue().status_code,409)
            first=self.client.get("/api/print-agent/test/next",headers=HEADERS)
            self.assertEqual(first.status_code,200,first.data)
            self.assertEqual(first.json["kind"],"onix-test-only")
            self.assertEqual(first.json["printer"],"ONIX-TEST-XP460B")
            self.assertEqual(self.client.get("/api/print-agent/test/next",
                headers=HEADERS).status_code,204)
            self.assertEqual(self.client.post("/api/print-agent/test/ack",json={
                "job_id":first.json["job_id"],"status":"printed"},headers=HEADERS).status_code,400)
            done=self.client.post("/api/print-agent/test/ack",json={
                "job_id":first.json["job_id"],"status":"submitted"},headers=HEADERS)
            self.assertEqual(done.status_code,200,done.data)
            self.assertEqual(self.client.post("/api/print-agent/test/ack",json={
                "job_id":first.json["job_id"],"status":"submitted"},headers=HEADERS).status_code,409)
            self.assertEqual(self.client.get("/api/print-agent/test/next",
                headers=HEADERS).status_code,204)
            self.assertEqual(query_one("SELECT status FROM onix_test_print_jobs WHERE id=?",
                (first.json["job_id"],))[0],"submitted")

    def test_uncertain_blocks_reprint(self):
        with patch.dict(os.environ,{"ONIX_PRINT_AGENT_TOKEN":TEST_SECRET}):
            self.assertEqual(self._enqueue().status_code,303)
            job=self.client.get("/api/print-agent/test/next",headers=HEADERS).json
            self.assertEqual(self.client.post("/api/print-agent/test/ack",json={
                "job_id":job["job_id"],"status":"uncertain"},headers=HEADERS).status_code,200)
            self.assertEqual(self._enqueue().status_code,409)

    def test_agent_ignores_non_test_job_and_never_prints(self):
        class FakeSpooler:
            PRINTER_ENUM_LOCAL=2
            PRINTER_ENUM_CONNECTIONS=4
            def __init__(self):
                self.calls=[]
            def EnumPrinters(self,*args):
                return [{"pPrinterName":"ONIX-TEST-XP460B"}]
            def GetDefaultPrinter(self):
                return "FBS-XP460B"
        fake=FakeSpooler()
        with patch("onix_print_agent._http",return_value={
            "job_id":"a"*36,"kind":"wb-order","printer":"ONIX-TEST-XP460B"}), \
             patch("onix_print_agent.submit_one_test_label") as write:
            with self.assertRaises(onix_print_agent.PrintAgentError):
                onix_print_agent.run_once(onix_print_agent.DEFAULT_SERVER,TEST_SECRET,fake)
            write.assert_not_called()

    def test_agent_sends_one_job_and_ack_submitted(self):
        class FakeSpooler:
            PRINTER_ENUM_LOCAL=2
            PRINTER_ENUM_CONNECTIONS=4
            def EnumPrinters(self,*args):
                return [{"pPrinterName":"ONIX-TEST-XP460B"}]
            def GetDefaultPrinter(self):
                return "FBS-XP460B"
        fake=FakeSpooler()
        returned={"job_id":"b"*36,"kind":"onix-test-only","printer":"ONIX-TEST-XP460B"}
        with patch("onix_print_agent._http",side_effect=[returned,{"ok":True}]) as http, \
             patch("onix_print_agent.submit_one_test_label") as writer:
            onix_print_agent.run_once(onix_print_agent.DEFAULT_SERVER,TEST_SECRET,fake)
            writer.assert_called_once_with(fake,"ONIX-TEST-XP460B")
            self.assertEqual(http.call_count,2)
            self.assertEqual(http.call_args.kwargs["payload"]["status"],"submitted")


if __name__=="__main__":
    unittest.main()
