"""Offline safety tests: exact original WB page, preview, and one-time RAW spool."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
import onix_wb_label_pilot as pilot


class FakeSpooler:
    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4
    def __init__(self, default="Xprinter XP-460B"):
        self.default = default
        self.calls = []
        self.writes = []
    def EnumPrinters(self, flags, server, level):
        return [{"pPrinterName": "ONIX-TEST-XP460B"},
                {"pPrinterName": "Xprinter XP-460B"}]
    def GetDefaultPrinter(self):
        return self.default
    def OpenPrinter(self, name):
        self.calls.append(("open",name))
        return "fake"
    def StartDocPrinter(self, handle, level, details):
        self.calls.append(("doc",details))
    def StartPagePrinter(self, handle):
        self.calls.append(("page",handle))
    def WritePrinter(self, handle, raw):
        self.writes.append(raw)
        return len(raw)
    def EndPagePrinter(self, handle):
        pass
    def EndDocPrinter(self, handle):
        pass
    def ClosePrinter(self, handle):
        pass


def product(qty, sku="1234_TEST"):
    return (f"{qty} шт.\nТовар для тестов\n"
            "Артикул WB: 1023996565\n"
            "Баркод: 2050291804984\n"
            f"Артикул:\n{sku}")


def pdf_fixture(path, labels, extra_unknown=False):
    document = fitz.open()
    sheet = document.new_page(width=220,height=190)
    sheet.insert_text((10,12),product(len(labels)),fontsize=9)
    for code in labels:
        sticker = document.new_page(width=113.39, height=85.04)  # 40x30 mm
        sticker.insert_text((5,13),"WB "+code,fontsize=9)
        for x in range(3,105,5):
            sticker.draw_rect(fitz.Rect(x,24,x+2,68), color=(0,0,0), fill=(0,0,0))
    if extra_unknown:
        unknown = document.new_page(width=113.39,height=85.04)
        unknown.insert_text((5,14),"UNKNOWN",fontsize=9)
    document.save(path)
    document.close()


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.pdf = Path(self.temp.name)/"WB_today.pdf"
        self.code = "59299122174"
        pdf_fixture(self.pdf,[self.code])

    def test_preview_is_only_local_and_correct_page(self):
        png, review, data = pilot.write_review(self.pdf,self.code)
        self.assertTrue(png.exists())
        self.assertTrue(review.exists())
        self.assertEqual(data["page"],2)
        self.assertEqual(data["article"],"1234_TEST")
        self.assertEqual(data["status"],"preview_ready")
        self.assertTrue(png.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(data["pdf_sha256"],
                         __import__("hashlib").sha256(self.pdf.read_bytes()).hexdigest())
        with self.assertRaises(pilot.PilotError):
            pilot.write_review(self.pdf,self.code)  # cannot silently overwrite

    def test_tspl_one_original_label_only_once(self):
        pilot.write_review(self.pdf,self.code)
        device=FakeSpooler()
        outcome=pilot.print_review(self.pdf,self.code,self.code,device)
        self.assertEqual(outcome["wb_code"],self.code)
        self.assertEqual(len(device.writes),1)
        self.assertEqual(device.calls[0],("open","ONIX-TEST-XP460B"))
        raw=device.writes[0]
        self.assertIn(b"BITMAP 0,0,40,240,0,",raw)
        self.assertTrue(raw.endswith(b"\r\nPRINT 1,1\r\n"))
        self.assertEqual(raw.count(b"PRINT 1,1"),1)
        _,journal=pilot.paths_for(self.pdf,self.code)
        self.assertEqual(json.loads(journal.read_text(encoding="utf8"))["status"],"submitted")
        with self.assertRaises(pilot.PilotError):
            pilot.print_review(self.pdf,self.code,self.code,device)
        self.assertEqual(len(device.writes),1)

    def test_default_printer_cannot_receive_wb_label(self):
        pilot.write_review(self.pdf,self.code)
        device=FakeSpooler(default="ONIX-TEST-XP460B")
        with self.assertRaises(pilot.PrintPilotError):
            pilot.print_review(self.pdf,self.code,self.code,device)
        self.assertEqual(device.writes,[])

    def test_explicit_confirmation_required(self):
        pilot.write_review(self.pdf,self.code)
        device=FakeSpooler()
        with self.assertRaises(pilot.PilotError):
            pilot.print_review(self.pdf,self.code,"59298125228",device)
        self.assertEqual(device.writes,[])

    def test_changed_pdf_rejected_after_preview(self):
        pilot.write_review(self.pdf,self.code)
        with self.pdf.open("ab") as f:
            f.write(b"\n% changed after manager preview")
        device=FakeSpooler()
        with self.assertRaises(pilot.PilotError):
            pilot.print_review(self.pdf,self.code,self.code,device)
        self.assertEqual(device.writes,[])

    def test_unknown_page_blocks_all_printing(self):
        pdf_fixture(self.pdf,[self.code],extra_unknown=True)
        with self.assertRaises(pilot.PilotError):
            pilot.write_review(self.pdf,self.code)

    def test_duplicate_wb_sticker_blocks_all_printing(self):
        pdf_fixture(self.pdf,[self.code,self.code])
        with self.assertRaises(pilot.PilotError):
            pilot.write_review(self.pdf,self.code)

    def test_pdf_with_mismatched_quantity_blocks_printing(self):
        pdf_fixture(self.pdf,[self.code])
        # Change the product header to claim 2 pieces while one sticker exists.
        with fitz.open(self.pdf) as d:
            new = fitz.open()
            s = new.new_page(width=220,height=190)
            s.insert_text((10,12),product(2),fontsize=9)
            new.insert_pdf(d,from_page=1,to_page=1)
            new.save(self.pdf.with_name("mismatch.pdf"))
            new.close()
        with self.assertRaises(pilot.PilotError):
            pilot.write_review(self.pdf.with_name("mismatch.pdf"),self.code)

    def test_packing_white_padding_and_black_pixels(self):
        px=pilot.pack_monochrome(bytes([0,255,255,0]*20),4,20,4)
        self.assertEqual(len(px),40*240)
        self.assertEqual(px[:40], b"\xff"*40)
        self.assertNotEqual(px[110*40:111*40],b"\xff"*40)
        self.assertIn(b"BITMAP 0,0,40,240,0,",pilot.tspl_bitmap(px))

    def test_rejects_label_page_thats_a4(self):
        doc=fitz.open()
        sheet=doc.new_page(width=220,height=190)
        sheet.insert_text((10,12),product(1),fontsize=9)
        sticker=doc.new_page(width=595,height=842)
        sticker.insert_text((10,15),"WB "+self.code,fontsize=9)
        doc.save(self.pdf.with_name("big.pdf"))
        doc.close()
        with self.assertRaises(pilot.PilotError):
            pilot.write_review(self.pdf.with_name("big.pdf"),self.code)


if __name__=="__main__":
    unittest.main()
