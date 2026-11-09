"""READ_ACCESS operation budgets, SQL cost and safe diagnostic preservation."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import MagicMock,patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/migration'))
import migration_tool as m
spec=importlib.util.spec_from_file_location('read_access_launcher',ROOT/'deploy/migrate.py')
kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
SECRET='fake-sensitive-table-name-and-row-value'


class ReadAccessTests(unittest.TestCase):
    def test_one_table_probe_no_explicit_lock_or_sequence_metadata_dump(self):
        conn=MagicMock();conn.execute.return_value.fetchall.return_value=[('seq1',)]
        with patch.object(m,'source_tables',return_value=['employees','scans']) as tables,patch.object(m,'sequence_state') as state:
            self.assertEqual(m.preflight_read_access(conn),['employees','scans'])
        tables.assert_called_once_with(conn,preflight=True);state.assert_not_called()
        queries=[call.args[0].as_string() if isinstance(call.args[0],m.sql.Composable) else call.args[0] for call in conn.execute.call_args_list]
        self.assertEqual(sum('LIMIT 0' in query for query in queries),2)
        self.assertFalse(any('LOCK TABLE' in query for query in queries))
        self.assertFalse(any('pg_sequences' in query or 'count(*)' in query for query in queries))
        self.assertIn("SET LOCAL lock_timeout='1s'",queries)
        self.assertIn("SET LOCAL statement_timeout='3s'",queries)

    def test_lock_unavailable_is_proven_by_sqlstate_not_elapsed_time(self):
        with self.assertRaisesRegex(m.DiagnosticFailure,'RA_TABLE_LOCK:LOCK_UNAVAILABLE') as caught:
            with m.preflight_operation('RA_TABLE_READ'):raise m.psycopg.errors.LockNotAvailable(SECRET)
        self.assertNotIn(SECRET,str(caught.exception))
        with self.assertRaisesRegex(m.DiagnosticFailure,'RA_TABLE_READ:PERMISSION'):
            with m.preflight_operation('RA_TABLE_READ'):raise m.psycopg.errors.InsufficientPrivilege(SECRET)
        with self.assertRaisesRegex(m.DiagnosticFailure,'RA_TABLE_READ:UNKNOWN'):
            with m.preflight_operation('RA_TABLE_READ'):raise RuntimeError('lock timeout '+SECRET)

    def test_each_read_access_substage_has_a_hard_deadline(self):
        for stage in m.READ_ACCESS_LIMITS:
            def target(dsn):
                with m.at_stage('READ_ACCESS'):
                    with m.preflight_operation(stage):time.sleep(60)
            started=time.monotonic()
            with self.subTest(stage=stage),self.assertRaisesRegex(m.DiagnosticFailure,stage+':TIMEOUT') as caught:
                m.supervise_check_source(SECRET,total_timeout=2,stage_timeouts={stage:0.1},target=target)
            self.assertLess(time.monotonic()-started,2);self.assertNotIn(SECRET,str(caught.exception))

    def test_parent_read_access_budget_does_not_reset_per_table(self):
        def target(dsn):
            with m.at_stage('READ_ACCESS'):
                while True:
                    with m.preflight_operation('RA_TABLE_READ'):time.sleep(0.02)
        started=time.monotonic()
        with self.assertRaisesRegex(m.DiagnosticFailure,'(?:READ_ACCESS|RA_TABLE_READ):TIMEOUT'):
            m.supervise_check_source(SECRET,total_timeout=3,stage_timeouts={'READ_ACCESS':0.15,'RA_TABLE_READ':1},target=target)
        self.assertLess(time.monotonic()-started,1.5)

    def test_schema_and_list_have_separate_events_and_no_identifiers(self):
        conn=MagicMock();empty=MagicMock();empty.fetchone.return_value=None
        table_cursor=MagicMock();table_cursor.__iter__.return_value=iter([(name,) for name in sorted(m.REQUIRED)])
        conn.execute.side_effect=[empty]*5+[table_cursor]
        sink=MagicMock()
        with patch.object(m,'_stage_sink',sink):self.assertEqual(set(m.source_tables(conn,preflight=True)),m.REQUIRED)
        self.assertEqual([call.args[0] for call in sink.send.call_args_list],[('begin','RA_SCHEMA'),('end','RA_SCHEMA'),('begin','RA_TABLE_LIST'),('end','RA_TABLE_LIST')])

    def test_new_worker_codes_are_accepted_by_host_without_relaying_raw_text(self):
        for stage in m.READ_ACCESS_LIMITS:
            self.assertIn(stage,kit.PREFLIGHT_STAGES)
        marker='9'*32;name='grit-source-check-'+marker;output=io.StringIO()
        code="import sys;sys.stdin.read();print('GRIT_PREFLIGHT_STAGE:RA_TABLE_READ',file=sys.stderr,flush=True);print('GRIT_EXPORT_FAILED:RA_TABLE_LOCK:LOCK_UNAVAILABLE',file=sys.stderr,flush=True);print('"+SECRET+"',file=sys.stderr);sys.exit(1)"
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(kit.Stop,'RA_TABLE_LOCK; category LOCK_UNAVAILABLE') as caught:
            kit.run_preflight_container([sys.executable,'-c',code],'',name,marker,total_timeout=2,stopper=lambda *args:True)
        self.assertNotIn(SECRET,str(caught.exception));self.assertNotIn(SECRET,output.getvalue())
