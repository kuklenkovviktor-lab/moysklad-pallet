"""Encrypted original-PDF queue synthetic tests; no USB, WB API or production."""
import sqlite3
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import fitz

from services.printing.wb_original_encrypted_outbox import (
    initialize, stage_verified_pdf, claim_one, report, LabelSourceError,
)

KEY=b'S'*32
NOW=datetime(2026,11,27,14,0,tzinfo=timezone.utc)


def fixture_pdf():
    doc=fitz.open()
    page=doc.new_page(width=58*72/25.4,height=40*72/25.4)
    page.insert_text((16,24),'WB 59298125228',fontsize=10)
    raw=doc.tobytes()
    doc.close()
    return raw


def fake_parser():
    return {'valid_structure':True,'products':[{
        'article':'TEST-ART','product_barcode':'2050291804984',
        'stickers':[{'barcode':'59298125228','page':1}],
    }]}


class EncryptedOriginalQueueTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
          CREATE TABLE wb_fbs_orders(account_id TEXT,order_id TEXT,
             wb_status TEXT,local_status TEXT,synced_at TEXT);
          CREATE TABLE wb_fbs_stickers(account_id TEXT,order_id TEXT,barcode TEXT);
          CREATE TABLE wb_fbs_picking_queue(account_id TEXT,order_id TEXT);
          CREATE TABLE wb_fbs_order_skus(account_id TEXT,order_id TEXT,sku TEXT);
          CREATE TABLE fbs_catalog_barcodes(
             barcode TEXT,product_id TEXT,article TEXT,quantity REAL,is_pack INTEGER);
          CREATE TABLE replenishment_locations(id TEXT,label TEXT,active INTEGER);
          CREATE TABLE cell_sku_assignments(location_id TEXT,product_id TEXT);
        """)
        self.db.execute('INSERT INTO wb_fbs_orders VALUES(?,?,?,?,?)',
                        ('synthetic','o1','confirm','pending',NOW.isoformat()))
        self.db.execute('INSERT INTO wb_fbs_stickers VALUES(?,?,?)',
                        ('synthetic','o1','59298125228'))
        self.db.execute('INSERT INTO wb_fbs_picking_queue VALUES(?,?)',
                        ('synthetic','o1'))
        self.db.execute('INSERT INTO wb_fbs_order_skus VALUES(?,?,?)',
                        ('synthetic','o1','2050291804984'))
        self.db.execute('INSERT INTO fbs_catalog_barcodes VALUES(?,?,?,?,?)',
                        ('2050291804984','prod1','TEST-ART',1,0))
        self.db.execute('INSERT INTO replenishment_locations VALUES(?,?,?)',
                        ('loc1','А-05-1',1))
        self.db.execute('INSERT INTO cell_sku_assignments VALUES(?,?)',
                        ('loc1','prod1'))
        self.db.commit()
        initialize(self.db)
        self.pdf=fixture_pdf()

    def stage(self,**kw):
        params={'account_id':'synthetic','printer_id':'p1',
                'verified_lines':('А',),'key':KEY,'clock':NOW}
        params.update(kw)
        with patch('services.printing.wb_pdf_original_label.parse_wb_labels',
                   return_value=fake_parser()):
            return stage_verified_pdf(self.db,self.pdf,**params)

    def claim(self,**kw):
        params={'printer_id':'p1','key':KEY,'authorized_device':True}
        params.update(kw)
        return claim_one(self.db,**params)

    def test_stage_original_page_encrypted_and_audited(self):
        self.assertEqual(self.stage(),1)
        row=self.db.execute('SELECT state,payload_encrypted,payload_hash FROM '
                            'synthetic_original_print_outbox').fetchone()
        self.assertEqual(row[0],'QUEUED')
        self.assertNotIn(b'BITMAP 0,0',row[1])
        self.assertNotIn(self.pdf[:30],row[1])
        self.assertEqual(len(row[2]),64)
        self.assertEqual(self.db.execute('SELECT count(*) FROM '
                         'synthetic_original_print_audit').fetchone()[0],1)

    def test_one_claim_returns_original_bitmap_and_durable_status(self):
        self.stage()
        ticket,payload=self.claim()
        self.assertEqual(len(ticket),32)
        self.assertIn(b'BITMAP 0,0,58,320,0,',payload)
        self.assertTrue(payload.endswith(b'PRINT 1,1\r\n'))
        self.assertEqual(payload.count(b'PRINT 1,1'),1)
        self.assertEqual(self.db.execute('SELECT state FROM '
                         'synthetic_original_print_outbox').fetchone()[0],'CLAIMED')

    def test_only_authorized_device_may_claim_or_report(self):
        self.stage()
        with self.assertRaises(PermissionError):
            self.claim(authorized_device=False)
        self.assertEqual(self.db.execute('SELECT state FROM '
                         'synthetic_original_print_outbox').fetchone()[0],'QUEUED')
        ticket,_=self.claim()
        with self.assertRaises(PermissionError):
            report(self.db,printer_id='p1',ticket_id=ticket,
                   result='SUBMITTED',authorized_device=False)

    def test_claim_never_repeated(self):
        self.stage()
        self.claim()
        with self.assertRaisesRegex(LabelSourceError,'UNRESOLVED_CLAIM'):
            self.claim()

    def test_submitted_ack_is_not_printed(self):
        self.stage()
        ticket,_=self.claim()
        result=report(self.db,printer_id='p1',ticket_id=ticket,
                      result='SUBMITTED',authorized_device=True)
        self.assertEqual(result,'SUBMITTED')
        self.assertEqual(self.db.execute('SELECT state FROM '
                         'synthetic_original_print_outbox').fetchone()[0],'SUBMITTED')
        with self.assertRaisesRegex(LabelSourceError,'CLAIM_NOT_ACTIVE'):
            report(self.db,printer_id='p1',ticket_id=ticket,
                   result='SUBMITTED',authorized_device=True)

    def test_uncertain_report_blocks_retry(self):
        self.stage()
        ticket,_=self.claim()
        report(self.db,printer_id='p1',ticket_id=ticket,
               result='UNKNOWN',authorized_device=True)
        with self.assertRaisesRegex(LabelSourceError,'UNRESOLVED_CLAIM'):
            self.claim()

    def test_duplicate_order_never_requeued_after_ack(self):
        self.stage()
        ticket,_=self.claim()
        report(self.db,printer_id='p1',ticket_id=ticket,
               result='SUBMITTED',authorized_device=True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.stage()
        self.assertEqual(self.db.execute('SELECT count(*) FROM '
                         'synthetic_original_print_outbox').fetchone()[0],1)

    def test_unknown_key_never_decrypts_or_requeues(self):
        self.stage()
        with self.assertRaisesRegex(LabelSourceError,'CLAIMED_PAYLOAD_UNAVAILABLE'):
            self.claim(key=b'Z'*32)
        with self.assertRaisesRegex(LabelSourceError,'UNRESOLVED_CLAIM'):
            self.claim()

    def test_bad_printer_lines_fail_closed_without_queue(self):
        with self.assertRaisesRegex(LabelSourceError,'WRONG_PRINTER_LINE'):
            self.stage(verified_lines=('Б',))
        self.assertEqual(self.db.execute('SELECT count(*) FROM '
                         'synthetic_original_print_outbox').fetchone()[0],0)

    def test_other_device_cannot_report(self):
        self.stage()
        ticket,_=self.claim()
        with self.assertRaisesRegex(LabelSourceError,'CLAIM_NOT_ACTIVE'):
            report(self.db,printer_id='p2',ticket_id=ticket,
                   result='SUBMITTED',authorized_device=True)

    def test_invalid_encryption_key_cannot_queue(self):
        with self.assertRaisesRegex(ValueError,'ENCRYPTION_KEY_MUST_BE_32_BYTES'):
            self.stage(key=b'short')

    def test_staging_refuses_unresolved_batch(self):
        self.stage()
        with self.assertRaisesRegex(LabelSourceError,'PRINTER_ALREADY_HAS_UNRESOLVED_BATCH'):
            self.stage()


if __name__=='__main__':
    unittest.main()
