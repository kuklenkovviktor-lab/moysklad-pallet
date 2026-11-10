"""Issued credentials survive startup even when the issuing key is unavailable.

Default: isolated temporary SQLite. Optional CI PostgreSQL uses only a loopback
admin DSN and creates a fresh badge_ci_* database per test; never drops a DB.
All keys, codes and records in this suite are synthetic.
"""
import hashlib
import hmac
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
EMPLOYEES = ('viktor', 'anton', 'ilya', 'loader', 'denis')


class IssuedBadgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        dsn = ''
        admin = os.getenv('GRIT_BADGE_TEST_ADMIN_DSN', '')
        if admin:
            import psycopg
            from psycopg.conninfo import conninfo_to_dict, make_conninfo
            from psycopg import sql
            values = conninfo_to_dict(admin)
            if values.get('host') not in ('127.0.0.1', 'localhost') or values.get('dbname') != 'postgres':
                raise RuntimeError('Badge test admin must be an isolated loopback PostgreSQL')
            database = 'badge_ci_' + uuid.uuid4().hex
            with psycopg.connect(admin, autocommit=True) as connection:
                connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
            values['dbname'] = database
            dsn = make_conninfo(**values)
        self.environment = patch.dict(os.environ, {
            'WMS_BADGE_SECRET': 'synthetic-current-key',
            'WMS_TERMINAL_SECRET': 'synthetic-terminal-key',
            'DATABASE_URL': dsn,
            'WMS_REQUIRE_POSTGRES': '1' if dsn else '0',
            'WMS_DB_PATH': str(Path(self.tmp.name) / 'fixture.db'),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        spec = importlib.util.spec_from_file_location('badge_fixture_' + uuid.uuid4().hex, ROOT / 'wms.py')
        self.wms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wms)
        self.modules = patch.dict(sys.modules, {'wms': self.wms})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.addCleanup(self.close_pool)
        self.wms.init_wms_db()
        self.app = Flask(__name__)
        self.app.secret_key = 'synthetic-session-key'
        self.app.register_blueprint(self.wms.wms_bp)

    def close_pool(self):
        if self.wms._PG_POOL is not None:
            self.wms._PG_POOL.close()

    def historical_codes(self):
        issued = {}
        key = b'synthetic-retired-issuing-key'
        for employee in EMPLOYEES:
            qr = 'WMS-BADGE-' + hmac.new(key, ('employee:' + employee).encode(), hashlib.sha256).hexdigest()[:32]
            digest = hmac.new(key, ('employee-scanner:' + employee).encode(), hashlib.sha256).hexdigest()
            scanner = '91' + f'{int(digest, 16) % (10 ** 18):018d}'
            self.wms.execute('UPDATE employees SET badge_code=?,scanner_code=? WHERE id=?', (qr, scanner, employee))
            issued[employee] = (qr, scanner)
        return issued

    def rows(self):
        return self.wms.query_all('SELECT * FROM employees ORDER BY id')

    def test_existing_credentials_and_employee_settings_survive_two_initializations(self):
        self.historical_codes()
        self.wms.execute('UPDATE employees SET name=?,rate=?,target=?,active=? WHERE id=?', ('Kept name', 7.5, 31, 0, 'anton'))
        before = self.rows()
        self.wms.WMS_BADGE_SECRET = b'another-synthetic-current-key'
        self.wms.init_wms_db()
        self.wms.init_wms_db()
        self.assertEqual(self.rows(), before)

    def test_all_five_old_qr_and_scanner_logins_work_after_startup(self):
        issued = self.historical_codes()
        self.wms.init_wms_db()
        self.wms.init_wms_db()
        for employee, codes in issued.items():
            for code in codes:
                with self.subTest(employee=employee, kind='QR' if code.startswith('WMS-') else 'scanner'):
                    response = self.app.test_client().post('/api/auth/login', json={'badge_code': code})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json['employee']['id'], employee)

    def test_missing_fields_are_filled_independently(self):
        issued = self.historical_codes()
        self.wms.execute('UPDATE employees SET badge_code=NULL WHERE id=?', ('viktor',))
        self.wms.execute('UPDATE employees SET scanner_code=? WHERE id=?', ('', 'anton'))
        self.wms.execute('UPDATE employees SET badge_code=?,scanner_code=NULL WHERE id=?', ('', 'ilya'))
        self.wms.init_wms_db()
        viktor = self.wms.query_one('SELECT badge_code,scanner_code FROM employees WHERE id=?', ('viktor',))
        anton = self.wms.query_one('SELECT badge_code,scanner_code FROM employees WHERE id=?', ('anton',))
        ilya = self.wms.query_one('SELECT badge_code,scanner_code FROM employees WHERE id=?', ('ilya',))
        self.assertEqual(viktor[1], issued['viktor'][1])
        self.assertEqual(anton[0], issued['anton'][0])
        self.assertTrue(viktor[0] and anton[1] and all(ilya))
        before = self.rows()
        self.wms.init_wms_db()
        self.assertEqual(self.rows(), before)

    def test_fresh_seed_credentials_are_unique_and_usable(self):
        rows = self.wms.query_all('SELECT id,badge_code,scanner_code FROM employees')
        self.assertEqual(len(rows), 5)
        self.assertEqual(len({row[1] for row in rows}), 5)
        self.assertEqual(len({row[2] for row in rows}), 5)
        for employee, qr, scanner in rows:
            self.assertTrue(qr and scanner)
            response = self.app.test_client().post('/api/auth/login', json={'badge_code': scanner})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json['employee']['id'], employee)

    def test_inactive_historical_badge_is_not_reactivated(self):
        issued = self.historical_codes()
        self.wms.execute('UPDATE employees SET active=0 WHERE id=?', ('anton',))
        self.wms.init_wms_db()
        for code in issued['anton']:
            self.assertEqual(self.app.test_client().post('/api/auth/login', json={'badge_code': code}).status_code, 404)
        self.assertEqual(self.wms.query_one('SELECT COUNT(*) FROM shifts WHERE employee_id=?', ('anton',))[0], 0)

    def test_bootstrap_and_printing_use_stored_issued_codes(self):
        issued = self.historical_codes()
        self.wms.init_wms_db()
        response = self.app.test_client().get('/setup/viktor-8f4c2e91a7b6')
        self.assertEqual(response.status_code, 200)
        self.assertIn(issued['viktor'][0], response.get_data(as_text=True))
        self.assertNotIn('__VIKTOR_BADGE_CODE__', response.get_data(as_text=True))
        manager = self.app.test_client()
        with manager.session_transaction() as session:
            session['manager_web'] = True
        with patch('qrcode.QRCode') as factory:
            response = manager.get('/api/admin/badges')
        self.assertEqual(response.status_code, 200)
        printed = {call.args[0] for call in factory.return_value.add_data.call_args_list}
        self.assertEqual(printed, {codes[0] for codes in issued.values()})


if __name__ == '__main__':
    unittest.main()
