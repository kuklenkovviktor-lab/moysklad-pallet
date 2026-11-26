"""Hardware-free safety verification for GRIT one-shot USB 58x40 pilot.

These tests inject a fake Windows spooler. Never imports pywin32, opens a
USB device, spawns a printer process or connects to a production service.
"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from services.printing import grit_usb_print_pilot as pilot


class FakeSpooler:
    PRINTER_ENUM_LOCAL=2
    PRINTER_ENUM_CONNECTIONS=4

    def __init__(self, *, default="FBS-XP460B", printers=None,
                 port="USB001", byte_count=None, fail_on=None):
        self.default=default
        self.printers=printers if printers is not None else [
            "FBS-XP460B",pilot.TEST_PRINTER]
        self.port=port
        self.byte_count=byte_count
        self.fail_on=fail_on
        self.events=[]

    def _hit(self, operation):
        self.events.append(operation)
        if self.fail_on==operation:
            raise OSError("Synthetic spooler error")

    def EnumPrinters(self, flags, server, level):
        self._hit("enumerate")
        return [{"pPrinterName":name} for name in self.printers]

    def GetDefaultPrinter(self):
        self._hit("default")
        return self.default

    def OpenPrinter(self, name):
        self._hit("open")
        self.last_opened=name
        return "fake-handle"

    def GetPrinter(self, handle, level):
        self._hit("get")
        return {"pPortName":self.port, "pPrinterName":self.last_opened}

    def ClosePrinter(self,handle):
        self._hit("close")

    def StartDocPrinter(self,handle,level,info):
        self._hit("start_doc")
        self.job_info=info
        return 111

    def StartPagePrinter(self,handle):
        self._hit("start_page")

    def WritePrinter(self,handle,payload):
        self._hit("write")
        self.payload=payload
        return len(payload) if self.byte_count is None else self.byte_count

    def EndPagePrinter(self,handle):
        self._hit("end_page")

    def EndDocPrinter(self,handle):
        self._hit("end_doc")


class GRITUsb58x40Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        self.fake=FakeSpooler()

    def run_cmd(self,args,device=None):
        stdout,stderr=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
            rc=pilot.main(args,spooler=device or self.fake,state_dir=self.dir)
        return rc,stdout.getvalue(),stderr.getvalue()

    def _print(self,device=None,**kwargs):
        return self.run_cmd([
            "--printer",kwargs.get("printer",pilot.TEST_PRINTER),
            "--print-one","--confirm",kwargs.get("confirm",pilot.CONFIRM)],
            device)

    def test_fixed_payload_is_58x40_not_wb_and_one_copy(self):
        label=pilot.build_test_label()
        self.assertIn(b"SIZE 58 mm,40 mm",label)
        self.assertIn(b"GAP 2 mm,0 mm",label)
        self.assertIn(b"GRIT WMS",label)
        self.assertIn(b"SYNTHETIC",label)
        self.assertIn(b"NOT WB",label)
        self.assertIn(pilot.TEST_QR.encode("ascii"),label)
        self.assertEqual(label.count(b"PRINT 1,1"),1)
        self.assertNotIn(b"GRIT-PRINTER:",label)
        self.assertNotIn(b"WILDBERRIES",label)

    def test_preview_never_opens_device_or_creates_journal(self):
        rc,out,_=self.run_cmd(["--preview"])
        self.assertEqual(rc,0)
        self.assertIn("NO PRINT",out)
        self.assertIn("SIZE 58 mm,40 mm",out)
        self.assertEqual(self.fake.events,[])
        self.assertFalse(pilot._journal_file(self.dir).exists())

    def test_status_without_attempt_is_read_only(self):
        rc,out,_=self.run_cmd(["--status"])
        self.assertEqual(rc,0)
        self.assertIn("Журнала нет",out)
        self.assertEqual(self.fake.events,[])

    def test_list_printers_is_read_only(self):
        rc,out,_=self.run_cmd(["--list"])
        self.assertEqual(rc,0)
        self.assertIn("FBS-XP460B (по умолчанию)",out)
        self.assertIn(pilot.TEST_PRINTER,out)
        self.assertNotIn("write",self.fake.events)

    def test_check_reads_usb_only_but_never_sends(self):
        rc,out,_=self.run_cmd(["--check","--printer",pilot.TEST_PRINTER])
        self.assertEqual(rc,0)
        self.assertIn("USB CHECK PASS",out)
        self.assertIn("USB001",out)
        self.assertFalse(pilot._journal_file(self.dir).exists())
        self.assertNotIn("start_doc",self.fake.events)
        self.assertNotIn("write",self.fake.events)

    def test_real_send_requires_explicit_confirmation(self):
        rc,_,err=self.run_cmd(["--printer",pilot.TEST_PRINTER,"--print-one"])
        self.assertEqual(rc,1)
        self.assertIn("подтверждение",err)
        self.assertEqual(self.fake.events,[])

    def test_rejects_any_non_allowlisted_printer(self):
        rc,_,err=self._print(printer="FBS-XP460B")
        self.assertEqual(rc,1)
        self.assertIn("Нужно явно указать --printer "+pilot.TEST_PRINTER,err)
        # Fail before touching the Windows printer/spooler or writing a claim.
        self.assertEqual(self.fake.events,[])
        self.assertFalse(pilot._journal_file(self.dir).exists())

    def test_rejects_test_printer_if_default(self):
        fake=FakeSpooler(default=pilot.TEST_PRINTER)
        rc,_,err=self._print(fake)
        self.assertEqual(rc,1)
        self.assertIn("по умолчанию",err)
        self.assertNotIn("write",fake.events)

    def test_rejects_missing_printer(self):
        fake=FakeSpooler(printers=["FBS-XP460B"])
        rc,_,err=self._print(fake)
        self.assertEqual(rc,1)
        self.assertIn("не обнаружена",err)
        self.assertNotIn("write",fake.events)

    def test_rejects_non_usb_port_and_network_printer(self):
        for port in ["WSD-12345","IP_192.168.1.23","COM1","USB001,USB002",""]:
            with self.subTest(port=port):
                fake=FakeSpooler(port=port)
                rc,_,err=self._print(fake)
                self.assertEqual(rc,1)
                self.assertIn("USB-порту",err)
                self.assertNotIn("write",fake.events)
                self.assertFalse(pilot._journal_file(self.dir).exists())

    def test_one_print_and_durable_submitted_not_printed(self):
        rc,out,err=self._print()
        self.assertEqual(rc,0,err)
        self.assertIn("SUBMITTED_TO_WINDOWS",out)
        self.assertEqual(self.fake.events.count("write"),1)
        self.assertEqual(self.fake.job_info,
                         (pilot.TEST_DOC,None,"RAW"))
        self.assertEqual(self.fake.payload,pilot.build_test_label())
        journal=json.loads(pilot._journal_file(self.dir).read_text("utf-8"))
        self.assertEqual(journal["state"],"SUBMITTED_TO_WINDOWS")
        self.assertEqual(journal["spool_job_id"],111)
        self.assertFalse(journal["physical_print_confirmed"])
        self.assertEqual(journal["label_mm"],[58,40])

    def test_repeated_print_attempt_does_not_write_again(self):
        self.assertEqual(self._print()[0],0)
        first=self.fake.events.count("write")
        rc,_,err=self._print()
        self.assertEqual(rc,1)
        self.assertIn("Повторная печать запрещена",err)
        self.assertEqual(self.fake.events.count("write"),first)

    def test_partial_spooler_write_is_unknown_and_blocks_retry(self):
        fake=FakeSpooler(byte_count=3)
        rc,_,err=self._print(fake)
        self.assertEqual(rc,1)
        self.assertIn("не приняла все байты",err)
        journal=json.loads(pilot._journal_file(self.dir).read_text("utf-8"))
        self.assertEqual(journal["state"],"UNKNOWN")
        self.assertEqual(fake.events.count("write"),1)
        self.assertEqual(self._print(fake)[0],1)
        self.assertEqual(fake.events.count("write"),1)

    def test_driver_exception_is_unknown_and_blocks_retry(self):
        fake=FakeSpooler(fail_on="write")
        rc,_,err=self._print(fake)
        self.assertEqual(rc,1)
        self.assertIn("Исход неизвестен",err)
        journal=json.loads(pilot._journal_file(self.dir).read_text("utf-8"))
        self.assertEqual(journal["state"],"UNKNOWN")
        self.assertEqual(self._print(fake)[0],1)
        self.assertEqual(fake.events.count("write"),1)

    def test_invalid_user_confirmation_never_sends(self):
        rc,_,_=self._print(confirm="yes")
        self.assertEqual(rc,1)
        self.assertEqual(self.fake.events,[])

    def test_status_reads_only_completed_spooler_record(self):
        self.assertEqual(self._print()[0],0)
        n=len(self.fake.events)
        rc,out,_=self.run_cmd(["--status"])
        self.assertEqual(rc,0)
        self.assertIn("SUBMITTED_TO_WINDOWS",out)
        self.assertEqual(n,len(self.fake.events))


if __name__=="__main__":
    unittest.main()
