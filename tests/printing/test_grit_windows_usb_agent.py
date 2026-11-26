"""Fake-spooler only Windows bridge tests. No USB, network or pywin32."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.printing import grit_windows_usb_agent as agent
from services.printing import grit_usb_print_pilot as pilot


spec=importlib.util.spec_from_file_location(
    "grit_usb_fake_spooler",
    Path(__file__).resolve().parent/"test_grit_usb_print_pilot.py")
fixture=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
FakeSpooler=fixture.FakeSpooler

TICKET="f"*32


class FakeAPI:
    def __init__(self, *, waiting=True, reject=False, failed_report=False):
        self.waiting=waiting
        self.reject=reject
        self.failed_report=failed_report
        self.claims=0
        self.reports=[]

    def __call__(self,method,path,body=None):
        if path.endswith("/ping"):
            return {"synthetic":True,"physical_printing":False,
                    "device":pilot.TEST_PRINTER,
                    "waiting":int(self.waiting)}
        if path.endswith("/claim"):
            self.claims += 1
            if not self.waiting:
                return {"synthetic":True,"physical_printing":False,"state":"EMPTY"}
            if self.reject:
                return {"synthetic":True,"physical_printing":False,"state":"REJECTED"}
            return {"synthetic":True,"physical_printing":False,
                    "state":"CLAIMED","ticket_id":TICKET,
                    "windows_printer":pilot.TEST_PRINTER}
        if path.endswith("/report"):
            self.reports.append(body)
            if self.failed_report:
                raise agent.PilotError("Synthetic lost bridge ACK")
            return {"synthetic":True,"physical_printing":False,
                    "state":"SUBMITTED" if body["result"]=="SUBMITTED_TO_WINDOWS"
                    else "UNKNOWN"}
        raise AssertionError("unexpected URL "+path)


class WindowsBridgeFakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        self.fake=FakeSpooler()
        self.api=FakeAPI()

    def test_local_label_is_fixed_one_58x40_synthetic_copy(self):
        payload=agent.build_ticket_label(TICKET)
        for item in (b"SIZE 58 mm,40 mm",b"GRIT WMS",b"NOT WB",
                     b"SYNTHETIC 1/1",b"GRIT-DEMO-"+b"f"*16):
            self.assertIn(item,payload)
        self.assertEqual(payload.count(b"PRINT 1,1"),1)
        self.assertNotIn(b"WB_FBS",payload)
        self.assertNotIn(b"SYNTH-WB-STICKER",payload)

    def test_malformed_ticket_never_becomes_tspl(self):
        for invalid in ("", "1"*31,"1"*33,
                        '";PRINT 20,20\r\n',None):
            with self.subTest(invalid=invalid):
                with self.assertRaises(agent.PilotError):
                    agent.build_ticket_label(invalid)

    def test_empty_queue_never_spools(self):
        api=FakeAPI(waiting=False)
        state,path=agent.run_once(api,self.fake,state_dir=self.dir)
        self.assertEqual((state,path),("EMPTY",None))
        self.assertNotIn("write",self.fake.events)
        self.assertEqual(list(self.dir.iterdir()),[])

    def test_empty_physical_command_is_failed_not_success(self):
        api=FakeAPI(waiting=False)
        self.assertEqual(agent.main(['--once','--confirm',agent.CONFIRM],
            api=api,spooler=self.fake,state_dir=self.dir),1)
        self.assertNotIn('write',self.fake.events)

    def test_rejected_claim_never_spools(self):
        api=FakeAPI(reject=True)
        state,path=agent.run_once(api,self.fake,state_dir=self.dir)
        self.assertEqual((state,path),("REJECTED",None))
        self.assertNotIn("write",self.fake.events)

    def test_check_bad_usb_does_not_even_claim(self):
        fake=FakeSpooler(default=pilot.TEST_PRINTER)
        with self.assertRaises(agent.PilotError):
            agent.run_once(self.api,fake,state_dir=self.dir)
        self.assertEqual(self.api.claims,0)
        self.assertNotIn("write",fake.events)

    def test_single_approved_claim_one_raw_write_and_ack(self):
        state,path=agent.run_once(self.api,self.fake,state_dir=self.dir)
        self.assertEqual(state,"SUBMITTED_TO_WINDOWS")
        self.assertEqual(path,self.dir/(TICKET+".json"))
        self.assertEqual(self.api.claims,1)
        self.assertEqual(self.fake.events.count("write"),1)
        self.assertEqual(self.fake.payload,agent.build_ticket_label(TICKET))
        self.assertEqual(self.api.reports,[
            {"ticket_id":TICKET,"result":"SUBMITTED_TO_WINDOWS","spool_job_id":111}])
        journal=json.loads(path.read_text("utf-8"))
        self.assertEqual(journal["state"],"SUBMITTED_TO_WINDOWS")
        self.assertFalse(journal["physical_print_confirmed"])

    def test_repeating_same_ticket_cannot_spool_again(self):
        agent.run_once(self.api,self.fake,state_dir=self.dir)
        with self.assertRaisesRegex(agent.PilotError,"Повторная печать запрещена"):
            agent.run_once(self.api,self.fake,state_dir=self.dir)
        self.assertEqual(self.fake.events.count("write"),1)
        journal=json.loads((self.dir/(TICKET+".json")).read_text("utf-8"))
        self.assertEqual(journal["state"],"SUBMITTED_TO_WINDOWS")

    def test_driver_error_becomes_unknown_no_retry(self):
        fake=FakeSpooler(fail_on="write")
        with self.assertRaisesRegex(agent.PilotError,"UNKNOWN"):
            agent.run_once(self.api,fake,state_dir=self.dir)
        self.assertEqual(fake.events.count("write"),1)
        self.assertEqual(self.api.reports[0]["result"],"UNKNOWN")
        journal=json.loads((self.dir/(TICKET+".json")).read_text("utf-8"))
        self.assertEqual(journal["state"],"UNKNOWN")
        with self.assertRaises(agent.PilotError):
            agent.run_once(self.api,fake,state_dir=self.dir)
        self.assertEqual(fake.events.count("write"),1)

    def test_partial_write_becomes_unknown(self):
        fake=FakeSpooler(byte_count=1)
        with self.assertRaises(agent.PilotError):
            agent.run_once(self.api,fake,state_dir=self.dir)
        self.assertEqual(self.api.reports[0]["result"],"UNKNOWN")

    def test_lost_server_ack_does_not_retry_spooler(self):
        api=FakeAPI(failed_report=True)
        with self.assertRaisesRegex(agent.PilotError,"ACK is uncertain"):
            agent.run_once(api,self.fake,state_dir=self.dir)
        self.assertEqual(self.fake.events.count("write"),1)
        with self.assertRaises(agent.PilotError):
            agent.run_once(api,self.fake,state_dir=self.dir)
        self.assertEqual(self.fake.events.count("write"),1)

    def test_confirmation_missing_or_wrong_prevents_claim(self):
        fake=FakeSpooler()
        for args in (["--once"],["--once","--confirm","yes"]):
            self.assertEqual(agent.main(args,api=self.api,spooler=fake,
                                        state_dir=self.dir),1)
        self.assertEqual(self.api.claims,0)
        self.assertNotIn("write",fake.events)

    def test_check_and_ping_do_not_print(self):
        self.assertEqual(agent.main(["--ping"],api=self.api,
                                    spooler=self.fake,state_dir=self.dir),0)
        self.assertEqual(agent.main(["--check"],api=self.api,
                                    spooler=self.fake,state_dir=self.dir),0)
        self.assertNotIn("write",self.fake.events)
        self.assertEqual(self.api.claims,0)

    def test_once_command_with_explicit_confirmation(self):
        self.assertEqual(agent.main(["--once","--confirm",agent.CONFIRM],
                                    api=self.api,spooler=self.fake,
                                    state_dir=self.dir),0)
        self.assertEqual(self.fake.events.count("write"),1)

    def test_api_never_accepts_remote_host_or_nonallowlisted_path(self):
        call=agent.make_api_call("T"*40)
        with self.assertRaises(agent.PilotError):
            call("GET","https://example.com/")
        with self.assertRaises(agent.PilotError):
            call("POST","/demo/printing/virtual-deliver",{})
        with self.assertRaises(agent.PilotError):
            agent.make_api_call("bad")


if __name__=="__main__":
    unittest.main()
