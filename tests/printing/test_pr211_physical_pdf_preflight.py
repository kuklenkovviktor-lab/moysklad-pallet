"""No-hardware regressions for one-shot ORIGINAL-LIKE SYNTHETIC PDF test.

Validates the REAL strict PDF parser and Windows physical one-shot journal,
but never makes SSH requests and NEVER opens a real Windows printer.
"""
import base64
from datetime import datetime
import hashlib
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase,main
import fitz

from fbs_pdf_labels import parse_wb_labels
from services.printing.wb_pdf_original_label import source_page_to_tspl
from services.printing.wb_original_usb_agent_synthetic import (
    validate_original_bitmap, decode_claim,
)
from deploy.pr211_physical_pdf_session import (
    KEY,pdf_bytes,TEST_STICKER,TEST_ARTICLE,TEST_SKU,QR_MATRIX,TEST_QR_CODE,
)
from deploy.pr211_physical_usb_client import execute, PilotError


class SyntheticSourcePDFTests(TestCase):
    def test_real_strict_parser_accepts_pdf_without_mocking(self):
        data=pdf_bytes()
        document=fitz.open(stream=data,filetype='pdf')
        try:
            parsed=parse_wb_labels(document)
            self.assertIsNotNone(parsed)
            self.assertTrue(parsed['valid_structure'],parsed['errors'])
            self.assertEqual(parsed['product_sheet_pages'],1)
            self.assertEqual(parsed['sticker_pages'],1)
            self.assertEqual(parsed['products'][0]['article'],TEST_ARTICLE)
            self.assertEqual(parsed['products'][0]['product_barcode'],TEST_SKU)
            self.assertEqual(parsed['products'][0]['stickers'][0]['barcode'],TEST_STICKER)
            self.assertEqual(document[1].get_text('text',sort=True).strip(),
                             'WB '+TEST_STICKER)
        finally:
            document.close()

    def test_actual_source_image_is_one_58x40_bitmap(self):
        data=pdf_bytes()
        raw=source_page_to_tspl(data,1)
        self.assertIn(b'BITMAP 0,0,58,320,0,',raw)
        self.assertEqual(raw.count(b'PRINT 1,1'),1)
        self.assertEqual(validate_original_bitmap(raw),raw)
        self.assertNotIn(b'GRIT-DEMO-',raw)
        self.assertEqual(len(KEY),32)
        self.assertEqual(len(QR_MATRIX),29)
        self.assertEqual(TEST_QR_CODE,'*Dc5xlawC')
        from wb_qr_codec import canonical_wb_sticker
        self.assertEqual(canonical_wb_sticker(TEST_QR_CODE),TEST_STICKER)

    def test_pdf_contains_real_2d_qr_and_not_linear_barcode(self):
        # Structural check is independent of text extraction. QR consists of
        # 29x29 squares with finder patterns and mandatory quiet zone.
        self.assertEqual(len(QR_MATRIX),29)
        self.assertTrue(all(len(row)==29 for row in QR_MATRIX))
        self.assertTrue(all(row=='0'*29 for row in QR_MATRIX[:4]))
        self.assertEqual(QR_MATRIX[4][4:11],'1111111')
        self.assertEqual(QR_MATRIX[4][18:25],'1111111')
        self.assertEqual(QR_MATRIX[24][4:11],'1111111')
        self.assertEqual(TEST_QR_CODE,'*Dc5xlawC')
        doc=fitz.open(stream=pdf_bytes(),filetype='pdf')
        try:
            # A literal sticker page has hundreds of vector QR modules and
            # only the original printed decimal human-readable WB number.
            page=doc[1]
            black_rectangles=page.get_drawings()
            self.assertGreater(len(black_rectangles),100)
            self.assertEqual(page.get_text('text',sort=True).strip(),
                             'WB '+TEST_STICKER)
        finally:
            doc.close()

    def test_encoded_claim_accepts_only_matching_source_hash(self):
        raw=source_page_to_tspl(pdf_bytes(),1)
        envelope=dict(synthetic=True,physical_printing=False,
                      state='CLAIMED',kind='ORIGINAL_PDF_BITMAP_SYNTHETIC_ONLY',
                      ticket_id='a'*32,
                      payload_sha256=hashlib.sha256(raw).hexdigest(),
                      payload_b64=base64.b64encode(raw).decode('ascii'))
        ticket,payload,digest=decode_claim(envelope)
        self.assertEqual(ticket,'a'*32)
        self.assertEqual(payload,raw)
        envelope['payload_sha256']='0'*64
        with self.assertRaisesRegex(PilotError,'DIGEST'):
            decode_claim(envelope)


class RealSourceMatchingAgainstSQLite(TestCase):
    def test_unmocked_pdf_matching_numeric_wb_code_to_order(self):
        import sqlite3
        from services.printing.wb_pdf_original_label import match_source_pdf
        from datetime import datetime, timezone
        now=datetime.now(timezone.utc)
        with sqlite3.connect(':memory:') as conn:
            conn.executescript("""
              CREATE TABLE wb_fbs_orders(account_id TEXT,order_id TEXT,
                wb_status TEXT,local_status TEXT,synced_at TEXT);
              CREATE TABLE wb_fbs_stickers(account_id TEXT,order_id TEXT,barcode TEXT);
              CREATE TABLE wb_fbs_picking_queue(account_id TEXT,order_id TEXT);
              CREATE TABLE wb_fbs_order_skus(account_id TEXT,order_id TEXT,sku TEXT);
              CREATE TABLE fbs_catalog_barcodes(barcode TEXT,product_id TEXT,
                article TEXT,quantity FLOAT,is_pack INTEGER);
              CREATE TABLE replenishment_locations(id TEXT,label TEXT,active INTEGER);
              CREATE TABLE cell_sku_assignments(location_id TEXT,product_id TEXT);
            """)
            conn.execute('INSERT INTO wb_fbs_orders VALUES (?,?,?,?,?)',
                ('synthetic','o1','confirm','pending',now.isoformat()))
            conn.execute('INSERT INTO wb_fbs_stickers VALUES (?,?,?)',
                ('synthetic','o1',TEST_STICKER))
            conn.execute('INSERT INTO wb_fbs_picking_queue VALUES (?,?)',
                ('synthetic','o1'))
            conn.execute('INSERT INTO wb_fbs_order_skus VALUES (?,?,?)',
                ('synthetic','o1',TEST_SKU))
            conn.execute('INSERT INTO fbs_catalog_barcodes VALUES (?,?,?,?,?)',
                (TEST_SKU,'p1',TEST_ARTICLE,1,0))
            conn.execute('INSERT INTO replenishment_locations VALUES (?,?,?)',
                ('l1','А-05-1',1))
            conn.execute('INSERT INTO cell_sku_assignments VALUES (?,?)',
                ('l1','p1'))
            jobs=match_source_pdf(pdf_bytes(),conn,account_id='synthetic',
                verified_lines=('А',),clock=now)
            self.assertEqual(len(jobs),1)
            self.assertEqual(jobs[0].sticker_code,TEST_STICKER)
            self.assertEqual(jobs[0].line,'А')
            self.assertEqual(jobs[0].page_index,1)


class FakeWindows:
    PRINTER_ENUM_LOCAL=2
    PRINTER_ENUM_CONNECTIONS=4
    def __init__(self):
        self.calls=0
    def EnumPrinters(self,*args):
        return [{'pPrinterName':'WORK-DEFAULT'},
                {'pPrinterName':'ONIX-TEST-XP460B'}]
    def GetDefaultPrinter(self):
        return 'WORK-DEFAULT'
    def OpenPrinter(self,name):
        assert name=='ONIX-TEST-XP460B'
        return 'FAKE'
    def GetPrinter(self,h,level):
        return {'pPortName':'USB001'}
    def ClosePrinter(self,h):
        pass
    def StartDocPrinter(self,h,level,info):
        return 111
    def StartPagePrinter(self,h):
        pass
    def WritePrinter(self,h,payload):
        self.calls+=1
        return len(payload)
    def EndPagePrinter(self,h):
        pass
    def EndDocPrinter(self,h):
        pass


class FakePopen:
    def __init__(self,response):
        self.response=response
        self.stdin=io.StringIO()
        self.stdout=io.StringIO(response+'\nPR211_SERVER_REPORT_SUBMITTED\n')
    def poll(self):
        return None
    def wait(self,timeout=None):
        return 0
    def kill(self):
        pass


class OnceOnlyFakeUsbTests(TestCase):
    def test_first_physical_attempt_one_raw_second_refused(self):
        raw=source_page_to_tspl(pdf_bytes(),1)
        claim=dict(synthetic=True,physical_printing=False,state='CLAIMED',
                   kind='ORIGINAL_PDF_BITMAP_SYNTHETIC_ONLY',
                   ticket_id='a'*32,payload_sha256=hashlib.sha256(raw).hexdigest(),
                   payload_b64=base64.b64encode(raw).decode('ascii'))
        response=json.dumps(dict(kind='PR211_SYNTHETIC_PHYSICAL_QR_PDF',
            order='SYNTH-PR211-PDF-PHYSICAL-1',sticker=TEST_STICKER,
            line='А',qr_payload=TEST_QR_CODE,symbol='QR',claim=claim))
        fake=FakeWindows()
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'one.json'
            def popen(*args,**kwargs):
                return FakePopen(response)
            self.assertEqual(execute(spooler=fake,popen=popen,state_path=marker),0)
            self.assertEqual(fake.calls,1)
            self.assertTrue(marker.exists())
            self.assertEqual(json.loads(marker.read_text())['state'],'SUBMITTED_TO_WINDOWS')
            with self.assertRaisesRegex(PilotError,'already registered'):
                execute(spooler=fake,popen=popen,state_path=marker)
            self.assertEqual(fake.calls,1)

    def test_wrong_printer_or_default_printer_fails_before_ssh(self):
        bad=FakeWindows()
        bad.GetDefaultPrinter=lambda:'ONIX-TEST-XP460B'
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(PilotError):
                execute(spooler=bad,popen=lambda *x,**y:None,
                        state_path=Path(tmp)/'one.json')
            self.assertEqual(bad.calls,0)


if __name__=='__main__':
    main()
