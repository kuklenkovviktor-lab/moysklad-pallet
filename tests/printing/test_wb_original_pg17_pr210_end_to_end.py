"""PR211 disposable PG17: original PDF -> encrypted queue -> actual PR210 RAW
spool function on FAKE Windows device -> ACK/UNKNOWN, no production.
"""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from unittest.mock import patch

import fitz
import psycopg

from services.printing.wb_original_pg17_outbox import (
    initialize, stage, claim, report, dispatch_fake_windows, LabelSourceError
)

DSN=os.environ.get('PR211_SYNTHETIC_DATABASE_URL','')
if (os.environ.get('GRIT_SYNTHETIC_PG_TEST')!='YES_ISOLATED'
        or not DSN.startswith(
          'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_')):
    raise SystemExit('ONLY_DISPOSABLE_PR211_POSTGRESQL_ALLOWED')
KEY=b'R'*32
NOW=datetime(2026,11,27,14,0,tzinfo=timezone.utc)


def conn():
    return psycopg.connect(DSN,autocommit=True)


class FakeWindowsSpooler:
    def __init__(self, fail=False):
        self.writes=[]
        self.fail=fail

    def OpenPrinter(self,name):
        assert name=='ONIX-TEST-XP460B'
        return 'fake'

    def StartDocPrinter(self,handle,level,info):
        return 111

    def StartPagePrinter(self,handle):
        pass

    def WritePrinter(self,handle,payload):
        self.writes.append(payload)
        if self.fail:
            raise OSError('synthetic USB uncertainty')
        return len(payload)

    def EndPagePrinter(self,handle):
        pass

    def EndDocPrinter(self,handle):
        pass

    def ClosePrinter(self,handle):
        pass


def source_pdf():
    doc=fitz.open()
    page=doc.new_page(width=58*72/25.4,height=40*72/25.4)
    page.insert_text((16,24),'WB 59298125228',fontsize=11)
    result=doc.tobytes()
    doc.close()
    return result


def fake_parser(_):
    return {'valid_structure':True,'products':[{
        'article':'SYNTH-ART','product_barcode':'2050291804984',
        'stickers':[{'barcode':'59298125228','page':1}]}]}


DDL=(
    'CREATE TABLE IF NOT EXISTS wb_fbs_orders(account_id TEXT,order_id TEXT,wb_status TEXT,local_status TEXT,synced_at TEXT)',
    'CREATE TABLE IF NOT EXISTS wb_fbs_stickers(account_id TEXT,order_id TEXT,barcode TEXT)',
    'CREATE TABLE IF NOT EXISTS wb_fbs_picking_queue(account_id TEXT,order_id TEXT)',
    'CREATE TABLE IF NOT EXISTS wb_fbs_order_skus(account_id TEXT,order_id TEXT,sku TEXT)',
    'CREATE TABLE IF NOT EXISTS fbs_catalog_barcodes(barcode TEXT,product_id TEXT,article TEXT,quantity DOUBLE PRECISION,is_pack INTEGER)',
    'CREATE TABLE IF NOT EXISTS replenishment_locations(id TEXT,label TEXT,active INTEGER)',
    'CREATE TABLE IF NOT EXISTS cell_sku_assignments(location_id TEXT,product_id TEXT)',
)


class OriginalPdfPG17WindowsEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with conn() as db:
            for statement in DDL:
                db.execute(statement)
            initialize(db)

    def setUp(self):
        self.db=conn()
        self.addCleanup(self.db.close)
        for table in ('synthetic_pr211_original_events',
                      'synthetic_pr211_original_jobs',
                      'cell_sku_assignments','replenishment_locations',
                      'fbs_catalog_barcodes','wb_fbs_order_skus',
                      'wb_fbs_picking_queue','wb_fbs_stickers','wb_fbs_orders'):
            self.db.execute('DELETE FROM '+table)
        self.db.execute('INSERT INTO wb_fbs_orders VALUES (%s,%s,%s,%s,%s)',
                        ('synthetic','o1','confirm','pending',NOW.isoformat()))
        self.db.execute('INSERT INTO wb_fbs_stickers VALUES (%s,%s,%s)',
                        ('synthetic','o1','59298125228'))
        self.db.execute('INSERT INTO wb_fbs_picking_queue VALUES (%s,%s)',
                        ('synthetic','o1'))
        self.db.execute('INSERT INTO wb_fbs_order_skus VALUES (%s,%s,%s)',
                        ('synthetic','o1','2050291804984'))
        self.db.execute('INSERT INTO fbs_catalog_barcodes VALUES (%s,%s,%s,%s,%s)',
                        ('2050291804984','p1','SYNTH-ART',1,0))
        self.db.execute('INSERT INTO replenishment_locations VALUES (%s,%s,%s)',
                        ('loc1','А-05-1',1))
        self.db.execute('INSERT INTO cell_sku_assignments VALUES (%s,%s)',
                        ('loc1','p1'))
        self.pdf=source_pdf()

    def stage(self, **changes):
        params=dict(account_id='synthetic',printer_id='p1',
                    verified_lines=('А',),key=KEY,clock=NOW)
        params.update(changes)
        with patch('services.printing.wb_pdf_original_label.parse_wb_labels',
                   side_effect=fake_parser):
            return stage(self.db,self.pdf,**params)

    def test_original_pdf_pg17_actual_pr210_one_raw_write(self):
        self.assertEqual(self.stage(),1)
        device=FakeWindowsSpooler()
        state=dispatch_fake_windows(self.db,printer_id='p1',key=KEY,
                                    device_authenticated=True,fake_spooler=device)
        self.assertEqual(state,'SUBMITTED')
        self.assertEqual(len(device.writes),1)
        self.assertEqual(device.writes[0].count(b'PRINT 1,1'),1)
        self.assertIn(b'BITMAP 0,0,58,320,0,',device.writes[0])
        self.assertEqual(dispatch_fake_windows(
            self.db,printer_id='p1',key=KEY,device_authenticated=True,
            fake_spooler=device),'EMPTY')
        self.assertEqual(len(device.writes),1)
        self.assertEqual(self.db.execute('SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
                         'SUBMITTED')
        self.assertEqual({r[0] for r in self.db.execute(
            'SELECT event FROM synthetic_pr211_original_events')},
            {'ORIGINAL_STAGED','CLAIMED_BEFORE_RAW','SUBMITTED'})

    def test_partial_raw_error_becomes_unknown_no_retry(self):
        self.stage()
        device=FakeWindowsSpooler(fail=True)
        with self.assertRaisesRegex(LabelSourceError,'WINDOWS_SPOOL_UNKNOWN'):
            dispatch_fake_windows(self.db,printer_id='p1',key=KEY,
                                  device_authenticated=True,fake_spooler=device)
        with self.assertRaisesRegex(LabelSourceError,'UNRESOLVED_PRINT'):
            dispatch_fake_windows(self.db,printer_id='p1',key=KEY,
                                  device_authenticated=True,fake_spooler=device)
        self.assertEqual(len(device.writes),1)
        self.assertEqual(self.db.execute(
            'SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],'UNKNOWN')

    def test_one_ticket_two_pg_connections_one_claim(self):
        self.stage()
        def contender(_):
            with conn() as independent:
                return claim(independent,printer_id='p1',key=KEY,
                             device_authenticated=True)
        outcomes=[]
        def safe(i):
            try:
                return contender(i)
            except LabelSourceError:
                return 'BLOCKED'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(safe,range(2)))
        self.assertEqual(sum(isinstance(x,tuple) for x in outcomes),1)
        self.assertIn('BLOCKED',outcomes)

    def test_bad_key_is_durable_claim_and_no_replay(self):
        self.stage()
        with self.assertRaisesRegex(LabelSourceError,'CLAIMED_PAYLOAD_INVALID'):
            claim(self.db,printer_id='p1',key=b'Z'*32,
                  device_authenticated=True)
        self.assertEqual(self.db.execute('SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
                         'CLAIMED')
        with self.assertRaisesRegex(LabelSourceError,'UNRESOLVED_PRINT'):
            claim(self.db,printer_id='p1',key=KEY,device_authenticated=True)

    def test_unauthorized_device_never_claims(self):
        self.stage()
        with self.assertRaises(PermissionError):
            claim(self.db,printer_id='p1',key=KEY,device_authenticated=False)
        self.assertEqual(self.db.execute('SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
                         'QUEUED')

    def test_wrong_printer_line_rejects_before_staging(self):
        with self.assertRaisesRegex(LabelSourceError,'WRONG_PRINTER_LINE'):
            self.stage(verified_lines=('Б',))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM synthetic_pr211_original_jobs').fetchone()[0],0)

    def test_duplicate_order_staged_once_even_after_ack(self):
        self.stage()
        ticket,payload=claim(self.db,printer_id='p1',key=KEY,
                             device_authenticated=True)
        report(self.db,printer_id='p1',ticket=ticket,result='SUBMITTED',
               device_authenticated=True)
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.stage()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM synthetic_pr211_original_jobs').fetchone()[0],1)

    def test_reject_non_autocommit_connection(self):
        with psycopg.connect(DSN,autocommit=False) as unsafe:
            with self.assertRaisesRegex(RuntimeError,'AUTOCOMMIT_CONNECTION'):
                claim(unsafe,printer_id='p1',key=KEY,
                      device_authenticated=True)


if __name__=='__main__':
    unittest.main()
