"""PR211 synthetic full HTTP E2E: PDF manager upload → WMS-like worker
session + signed printer QR → server-verified device → PG17 → PR210 Windows
RAW algorithm + permanent journal on fake USB. NEVER real WB/USB/prod.
"""
import hashlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime,timezone
from unittest.mock import patch

import fitz
import psycopg
from flask import Flask, session

from services.printing.wb_original_pg17_outbox import initialize as init_queue
from services.printing.wb_original_pg17_device_auth import initialize as init_devices
from services.printing.wb_original_print_http_synthetic import (
    make_isolated_original_print_blueprint,
)
from services.printing.wb_original_usb_agent_synthetic import (
    submit_one_synthetic_claim, PilotError,
)

DSN=os.getenv('PR211_SYNTHETIC_DATABASE_URL','')
ISOLATED=(os.getenv('GRIT_SYNTHETIC_PG_TEST')=='YES_ISOLATED'
          and DSN.startswith(
              'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_'))
KEY=b'PR211-ISOLATED-TEST-KEY-32-BYTES'
NOW=datetime.now(timezone.utc)
QR='GRIT-PRINTER:1:p1:'+'a'*48


def connect():
    return psycopg.connect(DSN,autocommit=True)


def pdf():
    d=fitz.open()
    p=d.new_page(width=220,height=330)
    # The synthetic product sheet is interpreted by the strict parser through
    # test fixtures, not a real customer/WB file.
    p.insert_text((12,24),'SYNTHETIC PRODUCT SHEET')
    p=d.new_page(width=58*72/25.4,height=40*72/25.4)
    p.insert_text((20,30),'WB 59298125228',fontsize=12)
    data=d.tobytes()
    d.close()
    return data


PARSER={'valid_structure':True,'products':[{
    'article':'SYNTH-ART','product_barcode':'2050291804984',
    'stickers':[{'barcode':'59298125228','page':2}],
}]}


class FakeWin32:
    PRINTER_ENUM_LOCAL=2
    PRINTER_ENUM_CONNECTIONS=4

    def __init__(self,fail=False):
        self.writes=[]
        self.fail=fail
        self.events=[]

    def EnumPrinters(self,*args):
        return [{'pPrinterName':'FBS-WORK'},{'pPrinterName':'ONIX-TEST-XP460B'}]

    def GetDefaultPrinter(self):
        return 'FBS-WORK'

    def OpenPrinter(self,name):
        if name!='ONIX-TEST-XP460B':
            raise AssertionError('wrong printer')
        return name

    def GetPrinter(self,handle,level):
        return {'pPortName':'USB001'}

    def ClosePrinter(self,handle):
        pass

    def StartDocPrinter(self,handle,level,info):
        return 111

    def StartPagePrinter(self,handle):
        pass

    def WritePrinter(self,handle,payload):
        self.writes.append(payload)
        if self.fail:
            raise OSError('fake USB uncertain')
        return len(payload)

    def EndPagePrinter(self,handle):
        pass

    def EndDocPrinter(self,handle):
        pass


@unittest.skipUnless(ISOLATED,'requires standalone disposable PostgreSQL 17')
class PR211ManagerWorkerDeviceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as conn:
            for ddl in (
                'CREATE TABLE IF NOT EXISTS wb_fbs_orders(account_id TEXT,order_id TEXT,wb_status TEXT,local_status TEXT,synced_at TEXT)',
                'CREATE TABLE IF NOT EXISTS wb_fbs_stickers(account_id TEXT,order_id TEXT,barcode TEXT)',
                'CREATE TABLE IF NOT EXISTS wb_fbs_picking_queue(account_id TEXT,order_id TEXT)',
                'CREATE TABLE IF NOT EXISTS wb_fbs_order_skus(account_id TEXT,order_id TEXT,sku TEXT)',
                'CREATE TABLE IF NOT EXISTS fbs_catalog_barcodes(barcode TEXT,product_id TEXT,article TEXT,quantity DOUBLE PRECISION,is_pack INTEGER)',
                'CREATE TABLE IF NOT EXISTS replenishment_locations(id TEXT,label TEXT,active INTEGER)',
                'CREATE TABLE IF NOT EXISTS cell_sku_assignments(location_id TEXT,product_id TEXT)',
                'CREATE TABLE IF NOT EXISTS grit_line_printers(printer_id TEXT PRIMARY KEY,name TEXT,lines_json TEXT,token_digest TEXT,enabled INTEGER)',
            ):
                conn.execute(ddl)
            init_queue(conn)
            init_devices(conn)

    def setUp(self):
        self.conn=connect()
        self.addCleanup(self.conn.close)
        for t in ('synthetic_pr211_original_events','synthetic_pr211_original_jobs',
                  'synthetic_pr211_print_devices','grit_line_printers',
                  'cell_sku_assignments','replenishment_locations','fbs_catalog_barcodes',
                  'wb_fbs_order_skus','wb_fbs_picking_queue','wb_fbs_stickers','wb_fbs_orders'):
            self.conn.execute('DELETE FROM '+t)
        self.conn.execute('INSERT INTO wb_fbs_orders VALUES(%s,%s,%s,%s,%s)',
                          ('synthetic','o1','confirm','pending',NOW.isoformat()))
        self.conn.execute('INSERT INTO wb_fbs_stickers VALUES(%s,%s,%s)',
                          ('synthetic','o1','59298125228'))
        self.conn.execute('INSERT INTO wb_fbs_picking_queue VALUES(%s,%s)',
                          ('synthetic','o1'))
        self.conn.execute('INSERT INTO wb_fbs_order_skus VALUES(%s,%s,%s)',
                          ('synthetic','o1','2050291804984'))
        self.conn.execute('INSERT INTO fbs_catalog_barcodes VALUES(%s,%s,%s,%s,%s)',
                          ('2050291804984','p1','SYNTH-ART',1,0))
        self.conn.execute('INSERT INTO replenishment_locations VALUES(%s,%s,%s)',
                          ('loc1','А-05-1',1))
        self.conn.execute('INSERT INTO cell_sku_assignments VALUES(%s,%s)',
                          ('loc1','p1'))
        self.conn.execute('INSERT INTO grit_line_printers VALUES(%s,%s,%s,%s,%s)',
                          ('p1','Принтер А','["А"]',hashlib.sha256(QR.encode()).hexdigest(),1))
        app=Flask(__name__)
        app.secret_key='pr211-synthetic-session-test-only'
        def mgr():
            return session.get('manager_web') is True
        def worker():
            if session.get('worker')!='demo1':
                return None
            return {'employee_id':'demo1','shift_id':'demo-shift-1',
                    'picking_active':session.get('picking') is True}
        app.register_blueprint(make_isolated_original_print_blueprint(
            connect,key=KEY,manager_guard=mgr,worker_guard=worker))
        self.app=app
        self.client=app.test_client()
        self.data=pdf()
        self.manager()
        self.manager_csrf=self.client.get('/synthetic/pr211/manager/session').json['csrf']

    def manager(self):
        with self.client.session_transaction() as s:
            s['manager_web']=True

    def worker(self):
        with self.client.session_transaction() as s:
            s['worker']='demo1'
            s['picking']=True
        return self.client.get('/synthetic/pr211/worker/session').json['csrf']

    def stage(self):
        with patch('services.printing.wb_pdf_original_label.parse_wb_labels',
                   return_value=PARSER):
            return self.client.post('/synthetic/pr211/manager/pdf/stage',
                data={'csrf':self.manager_csrf,'printer_id':'p1',
                      'file':(io.BytesIO(self.data),'synthetic.pdf')},
                content_type='multipart/form-data')

    def enroll(self):
        r=self.client.post('/synthetic/pr211/manager/devices/enroll',
            json={'csrf':self.manager_csrf,'printer_id':'p1'})
        self.assertEqual(r.status_code,201,r.json)
        return r.json

    def agent_api(self,identity):
        headers={'X-GRIT-Device-ID':identity['device_id'],
                 'Authorization':'Bearer '+identity['token']}
        def call(path,body):
            r=self.client.post(path,json=body,headers=headers)
            if r.status_code!=200:
                raise RuntimeError('agent HTTP status '+str(r.status_code))
            return r.json
        return call

    def scan(self,qr=QR):
        csrf=self.worker()
        return self.client.post('/synthetic/pr211/worker/printer-qr',
                json={'csrf':csrf,'qr':qr})

    def test_full_manager_pdf_worker_qr_device_http_pr210_fake_usb(self):
        self.assertEqual(self.stage().status_code,200)
        device=self.enroll()
        api=self.agent_api(device)
        self.assertEqual(api('/synthetic/pr211/agent/claim',{})['state'],'EMPTY')
        scanned=self.scan()
        self.assertEqual(scanned.status_code,200,scanned.json)
        self.assertEqual(scanned.json['released'],1)
        with tempfile.TemporaryDirectory() as tmp:
            win=FakeWin32()
            self.assertEqual(submit_one_synthetic_claim(
                api,win,journal_dir=tmp),'SUBMITTED_TO_WINDOWS')
            self.assertEqual(len(win.writes),1)
            self.assertIn(b'BITMAP 0,0,58,320,0,',win.writes[0])
            self.assertEqual(win.writes[0].count(b'PRINT 1,1'),1)
            self.assertEqual(submit_one_synthetic_claim(
                api,win,journal_dir=tmp),'EMPTY')
            self.assertEqual(len(win.writes),1)
        self.assertEqual(self.conn.execute(
            'SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
            'SUBMITTED')

    def test_unauthed_manager_cannot_upload_or_enroll(self):
        with self.client.session_transaction() as s:
            s.pop('manager_web',None)
        self.assertEqual(self.stage().status_code,403)
        self.assertEqual(self.client.post('/synthetic/pr211/manager/devices/enroll',
            json={'csrf':self.manager_csrf,'printer_id':'p1'}).status_code,403)

    def test_manager_csrf_required(self):
        r=self.client.post('/synthetic/pr211/manager/devices/enroll',
            json={'csrf':'invalid','printer_id':'p1'})
        self.assertEqual(r.status_code,403)

    def test_worker_qr_required_before_agent_can_claim(self):
        self.stage()
        device=self.enroll()
        self.assertEqual(self.agent_api(device)(
            '/synthetic/pr211/agent/claim',{})['state'],'EMPTY')

    def test_wrong_qr_does_not_release_queue(self):
        self.assertEqual(self.stage().status_code,200)
        self.assertEqual(self.scan(QR[:-1]+'z').status_code,403)
        self.assertEqual(self.conn.execute(
            'SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
            'STAGED')

    def test_inactive_picking_worker_cannot_scan(self):
        self.stage()
        self.worker()
        with self.client.session_transaction() as s:
            s['picking']=False
        r=self.client.post('/synthetic/pr211/worker/printer-qr',
            json={'csrf':self.client.get('/synthetic/pr211/manager/session').json['csrf'],
                  'qr':QR})
        self.assertEqual(r.status_code,403)

    def test_wrong_device_token_denied(self):
        self.stage()
        device=self.enroll()
        headers={'X-GRIT-Device-ID':device['device_id'],
                 'Authorization':'Bearer '+'x'*64}
        r=self.client.post('/synthetic/pr211/agent/claim',json={},headers=headers)
        self.assertEqual(r.status_code,401)

    def test_revoked_device_denied(self):
        device=self.enroll()
        r=self.client.post('/synthetic/pr211/manager/devices/revoke',
            json={'csrf':self.manager_csrf,'device_id':device['device_id']})
        self.assertEqual(r.json['revoked'],True)
        headers={'X-GRIT-Device-ID':device['device_id'],
                 'Authorization':'Bearer '+device['token']}
        self.assertEqual(self.client.post(
            '/synthetic/pr211/agent/claim',json={},headers=headers).status_code,401)

    def test_agent_payload_not_accessible_without_credential(self):
        self.stage()
        self.scan()
        self.assertEqual(self.client.post(
            '/synthetic/pr211/agent/claim',json={}).status_code,401)

    def test_manager_overview_omits_device_token_and_raw_pdf(self):
        self.stage()
        device=self.enroll()
        r=self.client.get('/synthetic/pr211/manager/overview')
        self.assertEqual(r.status_code,200)
        self.assertNotIn(device['token'],r.get_data(as_text=True))
        self.assertNotIn('payload_b64',r.get_data(as_text=True))

    def test_fake_usb_error_unknown_and_no_reprint(self):
        self.stage()
        device=self.enroll()
        self.scan()
        api=self.agent_api(device)
        with tempfile.TemporaryDirectory() as tmp:
            win=FakeWin32(fail=True)
            with self.assertRaisesRegex(PilotError,'RAW_OUTCOME_UNKNOWN'):
                submit_one_synthetic_claim(api,win,journal_dir=tmp)
            self.assertEqual(len(win.writes),1)
            with self.assertRaises(RuntimeError):
                submit_one_synthetic_claim(api,win,journal_dir=tmp)
            self.assertEqual(len(win.writes),1)
        self.assertEqual(self.conn.execute(
            'SELECT state FROM synthetic_pr211_original_jobs').fetchone()[0],
            'UNKNOWN')


if __name__=='__main__':
    unittest.main()
