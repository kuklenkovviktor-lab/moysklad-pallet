"""Lost-update reproducer: synthetic MoySklad transport, SQLite and isolated PG."""
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

for key in ('SECRET_KEY','WMS_BADGE_SECRET','WMS_TERMINAL_SECRET'):
    os.environ.setdefault(key,'receiving-isolated-test-only')
import app
import wms
from receiving_lock import receipt_write_lock


class ReceivingConcurrencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory()
        cls.saved=(wms.DATABASE_URL,wms.DB_PATH,wms._PG_POOL)
        wms.DATABASE_URL=os.getenv('GRIT_RECEIVING_TEST_DSN','')
        wms.DB_PATH=cls.tmp.name+'/receiving.db';wms._PG_POOL=None
        if wms.DATABASE_URL:
            with wms.db_connect() as conn: conn.execute('SELECT 1')

    @classmethod
    def tearDownClass(cls):
        if wms._PG_POOL: wms._PG_POOL.close()
        wms.DATABASE_URL,wms.DB_PATH,wms._PG_POOL=cls.saved
        cls.tmp.cleanup()

    def setUp(self):
        self.quantities={'receipt':10,'other':10}
        self.product={'meta':{'href':'https://fixture.invalid/product'}}
        self.pack={'quantity':5}

    def get(self, receipt):
        value=self.quantities[receipt]
        time.sleep(.04)  # Without the receipt lock both writers read 10.
        return {'applicable':False,'positions':{'rows':[{'id':'position','quantity':value,'assortment':self.product}]}}

    def put(self, url, **kwargs):
        receipt='other' if '/other/' in url else 'receipt'
        self.quantities[receipt]=kwargs['json']['quantity']
        return type('Response',(),{'raise_for_status':lambda self:None})()

    def test_parallel_physical_pallets_both_increment_existing_position(self):
        barrier=threading.Barrier(2)
        def receive(_):
            barrier.wait(timeout=5)
            return app.add_or_increment_position('receipt',self.product,self.pack)
        with patch.object(app,'get_receipt',side_effect=self.get),patch.object(app,'headers',return_value={}),patch.object(app.requests,'put',side_effect=self.put):
            with ThreadPoolExecutor(2) as pool: results=list(pool.map(receive,range(2)))
        self.assertEqual(self.quantities['receipt'],20)
        self.assertEqual(sorted(x['total'] for x in results),[15,20])

    def test_different_receipts_do_not_block_each_other(self):
        barrier=threading.Barrier(2)
        def lookup(receipt):
            barrier.wait(timeout=5)
            return self.get(receipt)
        with patch.object(app,'get_receipt',side_effect=lookup),patch.object(app,'headers',return_value={}),patch.object(app.requests,'put',side_effect=self.put):
            with ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda receipt:app.add_or_increment_position(receipt,self.product,self.pack),['receipt','other']))
        self.assertEqual([x['total'] for x in results],[15,15])

    def test_remote_failure_releases_lock_for_following_request(self):
        with patch.object(app,'get_receipt',side_effect=TimeoutError('fixture timeout')):
            with self.assertRaises(TimeoutError): app.add_or_increment_position('receipt',self.product,self.pack)
        with patch.object(app,'get_receipt',side_effect=self.get),patch.object(app,'headers',return_value={}),patch.object(app.requests,'put',side_effect=self.put):
            self.assertEqual(app.add_or_increment_position('receipt',self.product,self.pack)['total'],15)

    def test_completion_waits_for_document_lock_before_reading_summary(self):
        entered=threading.Event();finished=threading.Event()
        def finish():
            entered.set()
            with app.app.test_request_context():
                app.finish_receipt('receipt')
            finished.set()
        with patch.object(app,'has_unresolved_operations',return_value=False),patch.object(app,'get_receipt',side_effect=self.get),patch.object(app,'query_one',return_value=(0,)),patch.object(app,'complete_receipt',return_value={'id':'receipt'}),patch.object(app,'get_session_employee',return_value=None),patch.object(app,'backup_now'):
            with receipt_write_lock('receipt'):
                thread=threading.Thread(target=finish);thread.start();entered.wait(timeout=5)
                self.assertFalse(finished.wait(timeout=.1))
            thread.join(timeout=10)
            self.assertTrue(finished.is_set())
