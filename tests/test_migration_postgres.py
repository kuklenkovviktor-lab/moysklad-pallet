"""Explicitly opt-in tests on a disposable PostgreSQL supplied by Docker runner."""
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

try:
    import migration_tool as m
except ImportError:
    m=None


@unittest.skipUnless(m and os.getenv('GRIT_MIGRATION_TEST_ADMIN_DSN'), 'Requires isolated migration Docker runner')
class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.admin=os.environ['GRIT_MIGRATION_TEST_ADMIN_DSN']
        self.source='source_'+uuid.uuid4().hex[:12]
        with m.psycopg.connect(self.admin,autocommit=True) as conn:
            conn.execute(m.sql.SQL('CREATE DATABASE {}').format(m.sql.Identifier(self.source)))
        values=m.conninfo_to_dict(self.admin);values['dbname']=self.source
        self.dsn=m.make_conninfo(**values)
        with m.connection(self.dsn) as conn:
            conn.execute('CREATE TABLE employees(id text PRIMARY KEY,name text)')
            conn.execute('CREATE TABLE shifts(id text PRIMARY KEY,employee_id text REFERENCES employees,ended_at text)')
            conn.execute('CREATE TABLE activities(id text PRIMARY KEY,shift_id text REFERENCES shifts,employee_id text,ended_at text)')
            conn.execute('CREATE TABLE scans(id text PRIMARY KEY,shift_id text REFERENCES shifts,employee_id text,barcode text,product_barcode text)')
            conn.execute('CREATE TABLE operation_events(id text PRIMARY KEY,employee_id text,shift_id text,quantity int)')
            conn.execute("INSERT INTO employees VALUES ('e1','Тестовый сотрудник')")
            conn.execute("INSERT INTO shifts VALUES ('shift1','e1',NULL)")
            conn.execute("INSERT INTO activities VALUES ('a1','shift1','e1',NULL)")
            conn.execute("INSERT INTO scans VALUES ('scan1','shift1','e1','WB-test','SKU-test')")
            conn.execute("INSERT INTO operation_events VALUES ('event1','e1','shift1',1)")
        self.bundle=Path(self.tmp.name)/'bundle'
        self.target='restore_check_'+uuid.uuid4().hex[:12]

    def test_export_trial_restore_matches_all_rows_and_source_unchanged(self):
        before=m.snapshot(self.dsn)
        m.export(self.dsn,self.bundle)
        self.assertEqual(before,m.snapshot(self.dsn))
        restored=m.trial_restore(self.admin,self.bundle,self.target)
        self.assertEqual(m.compare(before,restored),[])
        self.assertEqual(restored['tables']['scans']['rows'],1)
        self.assertEqual(restored['tables']['scans']['sha256'],before['tables']['scans']['sha256'])

    def test_checksum_corruption_refused_before_target_creation(self):
        m.export(self.dsn,self.bundle)
        with (self.bundle/'database.dump').open('ab') as output: output.write(b'corrupt')
        with self.assertRaises(m.Refused): m.trial_restore(self.admin,self.bundle,self.target)
        with m.connection(self.admin) as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(self.target,)).fetchone())

    def test_existing_target_and_archive_cannot_be_overwritten(self):
        m.export(self.dsn,self.bundle)
        m.trial_restore(self.admin,self.bundle,self.target)
        with self.assertRaises(Exception): m.trial_restore(self.admin,self.bundle,self.target)
        with self.assertRaises(FileExistsError): m.export(self.dsn,self.bundle)
        self.assertEqual(m.verify_bundle(self.bundle)['tables']['employees']['rows'],1)

    def test_content_change_detected_even_when_row_counts_match(self):
        expected=m.snapshot(self.dsn)
        with m.connection(self.dsn) as conn: conn.execute("UPDATE employees SET name='Changed' WHERE id='e1'")
        actual=m.snapshot(self.dsn)
        self.assertEqual(actual['tables']['employees']['rows'],expected['tables']['employees']['rows'])
        self.assertIn('tables mismatch',m.compare(expected,actual))

    def test_readonly_snapshot_rejects_writes(self):
        with m.connection(self.dsn) as conn:
            m.begin_readonly(conn)
            with self.assertRaises(m.psycopg.errors.ReadOnlySqlTransaction):
                conn.execute("UPDATE employees SET name='not allowed'")
        self.assertEqual(m.snapshot(self.dsn)['tables']['employees']['rows'],1)

    def test_unsafe_target_name_is_refused(self):
        m.export(self.dsn,self.bundle)
        for target in ('grit','postgres','restore_check_a;DROP DATABASE grit'):
            with self.assertRaises(m.Refused): m.trial_restore(self.admin,self.bundle,target)

    def test_integrity_gate_reports_duplicate_and_orphan_scan(self):
        with m.connection(self.dsn) as conn:
            conn.execute("INSERT INTO scans VALUES ('scan2','shift1','other-employee','WB-test','SKU-test')")
        findings=m.snapshot(self.dsn)['integrity']
        self.assertEqual(findings['duplicate_wb_barcodes'],1)
        self.assertEqual(findings['orphan_scans'],1)

    def test_schema_change_is_detected(self):
        expected=m.snapshot(self.dsn)
        with m.connection(self.dsn) as conn: conn.execute('ALTER TABLE scans ADD COLUMN extra text')
        self.assertIn('tables mismatch',m.compare(expected,m.snapshot(self.dsn)))

    def test_remote_connection_requires_verified_tls_before_network_access(self):
        with self.assertRaisesRegex(m.Refused, 'verify-full'):
            m.connection('host=render.example.invalid dbname=grit sslmode=prefer')
