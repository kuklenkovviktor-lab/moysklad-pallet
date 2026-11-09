"""Safe error protocol and export failure retention. No external connections."""
import errno
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/migration'))
import migration_tool as m
import input_entrypoint as worker
spec=importlib.util.spec_from_file_location('diagnostic_launcher',ROOT/'deploy/migrate.py')
kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
SECRET='not-a-real-password-DO-NOT-PRINT'
DSN='postgresql://user:'+SECRET+'@render.example.invalid/grit?sslmode=verify-full'


class DiagnosticTests(unittest.TestCase):
    def test_structured_sqlstates_not_secret_word_guesses(self):
        cases=[(m.psycopg.errors.InvalidPassword(SECRET),'AUTHENTICATION'),
               (m.psycopg.errors.InsufficientPrivilege(SECRET),'PERMISSION'),
               (m.psycopg.errors.UndefinedTable(SECRET),'SCHEMA'),
               (m.psycopg.errors.DiskFull(SECRET),'DISK_FULL'),
               (OSError(errno.ENOSPC,SECRET),'DISK_FULL'),
               (RuntimeError('password ssl certificate '+SECRET),'UNKNOWN')]
        for error,category in cases:
            with self.subTest(category=category):
                self.assertEqual(m.failure_category(error),category)
                with self.assertRaises(m.DiagnosticFailure) as caught:
                    with m.at_stage('PG_DUMP'):raise error
                self.assertEqual(str(caught.exception),'GRIT_EXPORT_FAILED:PG_DUMP:'+category)
                self.assertNotIn(SECRET,str(caught.exception))

    def test_explicit_native_verifier_auth_disk_and_corruption_diagnostics(self):
        for stderr,category in [
            ('pg_dump: error: connection failed: SSL error: certificate verify failed: unable to get local issuer certificate','TLS'),
            ('psql: error: connection to server failed: FATAL:  password authentication failed for user "'+SECRET+'"','AUTHENTICATION'),
            ('pg_dump: error: query failed: ERROR:  permission denied for table '+SECRET,'PERMISSION'),
            ('pg_dump: error: could not write to output file: No space left on device','DISK_FULL'),
            ('pg_restore: error: input file does not appear to be a valid archive','INVALID_ARCHIVE'),
            ('pg_dump: error: unspecified error '+SECRET,'PROCESS_EXIT')]:
            error=subprocess.CalledProcessError(1,['pg_dump'],stderr=stderr.encode())
            self.assertEqual(m.failure_category(error),category)

    def test_pg_environment_is_explicit_and_psycopg_defaults_are_isolated(self):
        injected={'PGHOSTADDR':'192.0.2.1','PGSERVICE':'untrusted','PGPASSFILE':'/no/file','PGDATABASE':'wrong','PGPASSWORD':'wrong','PGSSLMODE':'disable'}
        with patch.dict(os.environ,injected):
            env=m.pg_environment(DSN)
            self.assertEqual(env['PGPASSWORD'],SECRET)
            self.assertEqual(env['PGDATABASE'],'grit')
            self.assertEqual(env['PGSSLMODE'],'verify-full')
            self.assertNotIn('PGHOSTADDR',env);self.assertNotIn('PGSERVICE',env);self.assertNotIn('PGPASSFILE',env)
            def connect(*args,**kwargs):
                self.assertFalse(any(key.startswith('PG') for key in os.environ))
                return object()
            with patch.object(m.psycopg,'connect',side_effect=connect):m.connection(DSN)
            self.assertEqual(os.environ['PGHOSTADDR'],injected['PGHOSTADDR'])

    def test_pg_dump_failure_keeps_zero_partial_and_never_finalizes(self):
        with tempfile.TemporaryDirectory() as temp:
            destination=Path(temp)/'new'
            def command(args,dsn,stage='RESTORE'):
                self.assertEqual(stage,'PG_DUMP')
                (destination/'database.dump.partial').touch()
                raise m.DiagnosticFailure('PG_DUMP','PROCESS_EXIT')
            from unittest.mock import MagicMock
            with patch.object(m,'client_major',return_value=17),patch.object(m,'source_connection',return_value=MagicMock()),patch.object(m,'source_snapshot',return_value='00000001-00000001-1'),patch.object(m,'sequence_state',return_value={}),patch.object(m,'pg_command',side_effect=command):
                with self.assertRaisesRegex(m.DiagnosticFailure,'PG_DUMP:PROCESS_EXIT'):m.export(DSN,destination)
            self.assertEqual((destination/'database.dump.partial').stat().st_size,0)
            self.assertFalse((destination/'database.dump').exists())

    def test_success_exit_with_empty_or_corrupt_output_is_not_success(self):
        from unittest.mock import MagicMock
        for data,category in [(b'','EMPTY_ARCHIVE'),(b'not a dump','INVALID_ARCHIVE')]:
            with tempfile.TemporaryDirectory() as temp:
                destination=Path(temp)/'new'
                def command(*args,**kwargs):(destination/'database.dump.partial').write_bytes(data)
                with patch.object(m,'client_major',return_value=17),patch.object(m,'source_connection',return_value=MagicMock()),patch.object(m,'source_snapshot',return_value='00000001-00000001-1'),patch.object(m,'sequence_state',return_value={}),patch.object(m,'pg_command',side_effect=command):
                    with self.assertRaisesRegex(m.DiagnosticFailure,'ARCHIVE_CHECK:'+category):m.export(DSN,destination)
                self.assertTrue((destination/'database.dump.partial').exists())
                self.assertFalse((destination/'database.dump').exists())

    def test_snapshot_denied_and_archive_creation_disk_full_have_exact_stage(self):
        from unittest.mock import MagicMock
        conn=MagicMock();conn.info.server_version=160000
        conn.execute.side_effect=m.psycopg.errors.InsufficientPrivilege(SECRET)
        with self.assertRaisesRegex(m.DiagnosticFailure,'SNAPSHOT:PERMISSION'):m.source_snapshot(conn,17)
        with patch.object(m.Path,'mkdir',side_effect=OSError(errno.ENOSPC,SECRET)):
            with self.assertRaisesRegex(m.DiagnosticFailure,'ARCHIVE_WRITE:DISK_FULL'):m.export(DSN,'/unused')

    def test_worker_and_launcher_never_relay_raw_error_or_sensitive_stdout(self):
        payload=json.dumps({'url':DSN,'action':'check-source'}).encode()
        with patch.object(worker.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(payload))),patch.object(worker,'check_source',side_effect=m.psycopg.OperationalError('unclassified '+DSN+' '+SECRET)):
            with self.assertRaises(SystemExit) as caught:worker.main()
        self.assertNotIn(SECRET,str(caught.exception));self.assertNotIn(DSN,str(caught.exception))
        error=subprocess.CalledProcessError(1,['docker'],stderr='GRIT_EXPORT_FAILED:PG_DUMP:PERMISSION\n'+SECRET)
        with patch.object(kit.subprocess,'run',side_effect=error):
            with self.assertRaisesRegex(kit.Stop,'PG_DUMP; category PERMISSION') as caught:kit.run(['docker'])
        self.assertNotIn(SECRET,str(caught.exception))

    def test_check_source_has_no_bundle_mount_and_secret_only_on_stdin(self):
        stdout='Source check passed: PostgreSQL 160010; native clients major 17; readable public tables 5. No dump created; full export may still encounter other errors.\n'
        with patch.object(kit,'secret',return_value=DSN),patch.object(kit,'run_preflight_container',return_value=SimpleNamespace(stdout=stdout)) as docker:
            kit.source_request('check-source')
        args=docker.call_args.args[0];payload=docker.call_args.args[1]
        self.assertNotIn(SECRET,str(args));self.assertNotIn('dst=/bundles',str(args))
        self.assertEqual(json.loads(payload)['action'],'check-source')
        self.assertIn('type=bind,src=/etc/ssl/certs,dst=/etc/ssl/certs,readonly',args)
        self.assertNotIn('-e',args)

class AdditionalFaultTests(unittest.TestCase):
    def test_native_pg_dump_credentials_never_appear_in_argv(self):
        dsn=worker.external_dsn(DSN)
        with patch.dict(os.environ,{'PGSSLMODE':'disable','PGSSLROOTCERT':'/wrong','PGHOSTADDR':'192.0.2.1'}),patch.object(m.subprocess,'run',return_value=SimpleNamespace(stdout=b'ok')) as run:
            self.assertEqual(m.pg_command(['pg_dump','--no-password'],dsn,stage='PG_DUMP'),b'ok')
        args=run.call_args.args[0];env=run.call_args.kwargs['env']
        self.assertNotIn(SECRET,str(args));self.assertEqual(env['PGPASSWORD'],SECRET)
        self.assertEqual(env['PGSSLMODE'],'verify-full')
        self.assertEqual(env['PGSSLROOTCERT'],'/etc/ssl/certs/ca-certificates.crt')
        self.assertNotIn('PGHOSTADDR',env)

    def test_pg_dump_disk_full_and_missing_tool_are_sanitized(self):
        for error,category in [(OSError(errno.ENOSPC,SECRET),'DISK_FULL'),(FileNotFoundError(errno.ENOENT,SECRET),'TOOL_MISSING')]:
            with patch.object(m.subprocess,'run',side_effect=error):
                with self.assertRaisesRegex(m.DiagnosticFailure,'PG_DUMP:'+category) as caught:
                    m.pg_command(['pg_dump'],DSN,stage='PG_DUMP')
            self.assertNotIn(SECRET,str(caught.exception))

    def test_unrecognized_worker_output_is_not_echoed(self):
        with patch.object(kit,'secret',return_value=DSN),patch.object(kit,'run_preflight_container',return_value=SimpleNamespace(stdout=SECRET)):
            with self.assertRaises(kit.Stop) as caught:kit.source_request('check-source')
        self.assertNotIn(SECRET,str(caught.exception))

    def test_schema_refusal_and_tls_words_do_not_guess_wrong_category(self):
        self.assertEqual(m.failure_category(m.SchemaRefused(SECRET)),'SCHEMA')
        self.assertEqual(m.failure_category(m.psycopg.OperationalError('unclassified password ssl '+SECRET)),'UNKNOWN')
        with self.assertRaises(m.Refused):worker.external_dsn('postgresql://user:fake@%2Ftmp/grit')
