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

    def test_sequence_and_identity_state_survives_and_changes_are_detected(self):
        with m.connection(self.dsn) as conn:
            conn.execute('CREATE TABLE serial_probe(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY)')
            conn.execute('INSERT INTO serial_probe DEFAULT VALUES')
            conn.execute("SELECT setval('serial_probe_id_seq',41,true)")
        before=m.export(self.dsn,self.bundle)
        restored=m.trial_restore(self.admin,self.bundle,self.target)
        self.assertEqual(before['sequences'],restored['sequences'])
        values=m.conninfo_to_dict(self.admin);values['dbname']=self.target
        target=m.make_conninfo(**values)
        with m.connection(target) as conn:
            self.assertEqual(conn.execute("SELECT nextval('serial_probe_id_seq')").fetchone()[0],42)
        self.assertIn('sequences mismatch',m.compare(before,m.snapshot(target)))

    def test_unverified_storage_is_refused_instead_of_omitted(self):
        with m.connection(self.dsn) as conn:
            conn.execute('CREATE SCHEMA extra; CREATE TABLE extra.unverified(id int)')
        with self.assertRaisesRegex(m.Refused,'Non-public'):m.snapshot(self.dsn)

    def test_large_objects_are_refused_until_content_audited(self):
        with m.connection(self.dsn) as conn:conn.execute('SELECT lo_create(0)')
        with self.assertRaisesRegex(m.Refused,'Large objects'):m.snapshot(self.dsn)

    def test_upgrade_startup_allows_version_change_but_never_lost_rows(self):
        before=m.snapshot(self.dsn)
        upgraded=dict(before,server_version=before['server_version']+10000)
        self.assertEqual(m.compare_restored_startup(before,upgraded),[])
        older=dict(before,server_version=before['server_version']-10000)
        self.assertTrue(m.compare_restored_startup(before,older))
        changed=json.loads(json.dumps(upgraded));changed['tables']['scans']['rows']=0
        self.assertTrue(m.compare_restored_startup(before,changed))

    def test_remote_connection_requires_verified_tls_before_network_access(self):
        with self.assertRaisesRegex(m.Refused, 'verify-full'):
            m.connection('host=render.example.invalid dbname=grit sslmode=prefer')

    def create_request_ledger(self):
        with m.connection(self.dsn) as conn:
            conn.execute('CREATE TABLE fbs_scan_requests (request_id TEXT PRIMARY KEY,scan_id TEXT NOT NULL,shift_id TEXT NOT NULL,employee_id TEXT NOT NULL,barcode TEXT NOT NULL,product_barcode TEXT NOT NULL)')

    def test_post_startup_comparison_accepts_real_pr93_empty_schema(self):
        baseline=m.snapshot(self.dsn)
        self.create_request_ledger()
        after=m.snapshot(self.dsn)
        self.assertEqual(m.compare_rollout(baseline,after),[])
        self.assertIn('tables mismatch',m.compare(baseline,after))

    def test_existing_request_ledger_must_preserve_all_records(self):
        self.create_request_ledger()
        with m.connection(self.dsn) as conn:
            conn.execute("INSERT INTO fbs_scan_requests VALUES ('r1','scan1','shift1','e1','WB-test','SKU-test')")
        baseline=m.snapshot(self.dsn)
        self.assertEqual(m.compare_rollout(baseline,m.snapshot(self.dsn)),[])
        with m.connection(self.dsn) as conn:
            conn.execute("UPDATE fbs_scan_requests SET barcode='changed'")
        self.assertTrue(m.compare_rollout(baseline,m.snapshot(self.dsn)))

    def test_post_startup_comparison_rejects_existing_schema_drift(self):
        baseline=m.snapshot(self.dsn)
        with m.connection(self.dsn) as conn:
            conn.execute('ALTER TABLE employees ADD COLUMN unexpected text')
        self.assertTrue(m.compare_rollout(baseline,m.snapshot(self.dsn)))
