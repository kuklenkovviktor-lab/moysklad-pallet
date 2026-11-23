"""Fault injection and concurrency on SQLite and optional isolated PostgreSQL.

GRIT_TEST_POSTGRES_DSN must point ONLY to the disposable database supplied by
our Docker test runner, never Render or HOSTKEY.
"""
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
import threading
import subprocess
import sys
from unittest.mock import patch

for key in ('SECRET_KEY', 'WMS_BADGE_SECRET', 'WMS_TERMINAL_SECRET'):
    os.environ.setdefault(key, 'isolated-fbs-test-only')
import wms
import fbs_transactions as tx
from fbs_skip_authorization import record_pending_item, pending_for_shift, consume_skip_code, issue_skip_code


class DurableFbsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.saved = (wms.DATABASE_URL, wms.DB_PATH, wms._DB_READY, wms._PG_POOL)
        wms.DATABASE_URL = os.getenv('GRIT_TEST_POSTGRES_DSN', '')
        wms.DB_PATH = cls.tmp.name + '/durable.db'
        wms._DB_READY = False
        wms._PG_POOL = None
        wms.init_wms_db()
        from flask import Flask
        cls.app = Flask(__name__)
        cls.app.secret_key = 'isolated-test-session'
        cls.app.testing = True
        cls.app.register_blueprint(wms.wms_bp)

    @classmethod
    def tearDownClass(cls):
        if wms._PG_POOL:
            wms._PG_POOL.close()
        wms.DATABASE_URL, wms.DB_PATH, wms._DB_READY, wms._PG_POOL = cls.saved
        cls.tmp.cleanup()

    def setUp(self):
        wms.execute('DELETE FROM pdf_fbs_orders')  # Only this class's disposable DB.
        self.employee = 'durable-' + uuid.uuid4().hex
        wms.execute('INSERT INTO employees (id,name,role) VALUES (?,?,?)', (self.employee, 'Isolated fixture', 'staff'))
        self.shift, _ = wms.ensure_active_shift(self.employee, 'picking')
        self.product = 'product-' + uuid.uuid4().hex
        record_pending_item(self.shift, self.employee, self.product, 'SKU-test', 'Fixture')
        self.barcode = 'wb-' + uuid.uuid4().hex
        self.request = str(uuid.uuid4())
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['employee_id'] = self.employee

    def commit(self, barcode=None, request=None):
        return tx.commit_scan(self.shift, self.employee, barcode or self.barcode, self.product,
                              'SKU-test', 'Fixture', None, 'normal', request or self.request, True)

    def count(self, table):
        return wms.query_one('SELECT count(*) FROM ' + table + ' WHERE shift_id=?', (self.shift,))[0]

    def payload(self):
        return dict(shift_id=self.shift, product_barcode=self.product, barcode=self.barcode, request_id=self.request)

    def test_lost_ack_retry_counts_once_but_closed_shift_revokes_api_session(self):
        first = self.client.post('/api/fbs/scan', json=self.payload())
        self.assertEqual(first.status_code, 200, first.json)
        tx.close_shift(self.shift, self.employee)
        # A previously accepted request remains idempotent at the ledger.
        self.assertTrue(self.commit())
        # The remote manager close must NOT keep an employee cookie valid,
        # even for a replay; reconciliation is possible through the ledger.
        retry = self.client.post('/api/fbs/scan', json=self.payload())
        self.assertEqual(retry.status_code, 401, retry.json)
        self.assertEqual(self.count('scans'), 1)
        self.assertEqual(wms.query_one("SELECT count(*) FROM operation_events WHERE shift_id=? AND event_type='scan'", (self.shift,))[0], 1)

    def test_request_id_cannot_be_rebound(self):
        self.commit()
        with self.assertRaises(ValueError):
            self.commit(barcode='different-order')
        self.assertEqual(self.count('scans'), 1)

    def test_two_orders_cannot_consume_one_pending_product(self):
        barrier = threading.Barrier(2)
        def attempt(i):
            barrier.wait(timeout=5)
            try:
                self.commit(barcode=self.barcode + str(i), request=str(uuid.uuid4()))
                return 'counted'
            except ValueError:
                return 'rejected'
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ['counted', 'rejected'])
        self.assertEqual(self.count('scans'), 1)
        self.assertIsNone(pending_for_shift(self.shift, self.employee))

    def test_same_request_concurrently_replays(self):
        barrier = threading.Barrier(2)
        def attempt(_):
            barrier.wait(timeout=5)
            return self.commit()
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), [False, True])
        self.assertEqual(self.count('scans'), 1)

    def test_event_failure_rolls_back_scan_request_and_pending_delete(self):
        with patch.object(tx, 'event', side_effect=RuntimeError('injected transaction failure')):
            with self.assertRaises(RuntimeError):
                self.commit()
        self.assertEqual(self.count('scans'), 0)
        self.assertEqual(self.count('fbs_scan_requests'), 0)
        self.assertIsNotNone(pending_for_shift(self.shift, self.employee))
        self.assertFalse(self.commit())

    def test_skip_event_failure_rolls_back_token_consumption(self):
        code, _ = issue_skip_code()
        with patch.object(tx, 'event', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                consume_skip_code(code, self.shift, self.employee)
        self.assertIsNotNone(pending_for_shift(self.shift, self.employee))
        consume_skip_code(code, self.shift, self.employee)
        record_pending_item(self.shift, self.employee, 'next-product', 'SKU', 'Next')
        with self.assertRaises(ValueError):
            consume_skip_code(code, self.shift, self.employee)
        self.assertEqual(pending_for_shift(self.shift, self.employee)['barcode'], 'next-product')

    def test_skip_and_scan_have_only_one_winner(self):
        code, _ = issue_skip_code()
        barrier = threading.Barrier(2)
        def attempt(i):
            barrier.wait(timeout=5)
            try:
                if i: self.commit()
                else: consume_skip_code(code, self.shift, self.employee)
                return True
            except ValueError:
                return False
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), [True, False])
        self.assertEqual(wms.query_one("SELECT count(*) FROM operation_events WHERE shift_id=? AND event_type IN ('scan','fbs_item_skip')", (self.shift,))[0], 1)

    def test_activity_switch_rollback_and_resume_preserve_pending(self):
        with patch.object(tx, 'event', side_effect=RuntimeError('injected failure')):
            with self.assertRaises(RuntimeError):
                wms.switch_activity(self.shift, self.employee, 'other')
        self.assertEqual(wms.current_activity(self.shift)['type'], 'picking')
        wms.switch_activity(self.shift, self.employee, 'other')
        with self.assertRaises(ValueError): self.commit()
        self.assertIsNotNone(pending_for_shift(self.shift, self.employee))
        wms.switch_activity(self.shift, self.employee, 'picking')
        self.commit()

    def test_concurrent_shift_start_returns_single_shift(self):
        employee = 'start-' + uuid.uuid4().hex
        wms.execute('INSERT INTO employees (id,name) VALUES (?,?)', (employee, 'Start fixture'))
        barrier = threading.Barrier(2)
        def attempt(_):
            barrier.wait(timeout=5)
            return wms.ensure_active_shift(employee, 'picking')[0]
        with ThreadPoolExecutor(2) as pool:
            ids = list(pool.map(attempt, range(2)))
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(wms.query_one('SELECT count(*) FROM shifts WHERE employee_id=?', (employee,))[0], 1)
        self.assertEqual(wms.query_one('SELECT count(*) FROM activities WHERE shift_id=?', (ids[0],))[0], 1)

    def test_conflicting_parallel_pdf_imports_cannot_silently_succeed(self):
        from pdf_fbs_registry import import_pdf_orders
        barcode = '59800123456'
        barrier = threading.Barrier(2)
        def attempt(i):
            report = {'valid_structure':True,'unique_sticker_barcodes':1,'products':[
                {'article':'SKU-'+str(i),'product_barcode':'203871909177'+str(i),
                 'stickers':[{'barcode':barcode}]}]}
            barrier.wait(timeout=5)
            try:
                import_pdf_orders(report, 'a'*64)
                return 'imported'
            except ValueError:
                return 'conflict'
        with ThreadPoolExecutor(2) as pool:
            self.assertCountEqual(list(pool.map(attempt, range(2))), ['imported','conflict'])
        self.assertEqual(wms.query_one('SELECT count(*) FROM pdf_fbs_orders')[0], 1)

    def test_restart_does_not_reenable_or_reset_employee(self):
        wms.execute('UPDATE employees SET active=0,rate=9,target=99,name=? WHERE id=?', ('Changed name','anton'))
        wms.init_wms_db()
        self.assertEqual(wms.query_one('SELECT active,rate,target,name FROM employees WHERE id=?',('anton',)), (0,9,99,'Changed name'))

    def test_unique_scan_index_failure_is_not_silently_ignored(self):
        original = wms.execute
        def fail_index(query, params=()):
            if 'CREATE UNIQUE INDEX IF NOT EXISTS idx_scans_barcode_unique' in query:
                raise RuntimeError('injected index failure')
            return original(query, params)
        with patch.object(wms, 'execute', side_effect=fail_index):
            with self.assertRaisesRegex(RuntimeError, 'WB-сканов'):
                wms.init_wms_db()

    def test_pending_survives_a_new_python_process(self):
        environment = dict(os.environ, DATABASE_URL=wms.DATABASE_URL, WMS_DB_PATH=wms.DB_PATH, PYTHONDONTWRITEBYTECODE='1')
        code = "from fbs_skip_authorization import pending_for_shift; import sys; assert pending_for_shift(sys.argv[1],sys.argv[2])['barcode']==sys.argv[3]"
        subprocess.run([sys.executable, '-c', code, self.shift, self.employee, self.product], env=environment, check=True, timeout=20)

    def test_repeated_product_scan_keeps_same_pending_and_no_order(self):
        record_pending_item(self.shift, self.employee, self.product, 'SKU-test', 'Fixture')
        self.assertEqual(self.count('fbs_pending_products'), 1)
        self.assertEqual(self.count('scans'), 0)
