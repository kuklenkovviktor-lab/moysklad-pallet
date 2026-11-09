"""Real local process stalls, fixed stage protocol, scoped stop (no Docker delete)."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/migration'))
import migration_tool as m
spec=importlib.util.spec_from_file_location('deadline_launcher',ROOT/'deploy/migrate.py')
kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
RESULT={'server_version':160000,'client_major':17,'tables_checked':5}
SECRET='fake-only-secret-that-must-never-be-printed'


class WorkerDeadlineTests(unittest.TestCase):
    def test_every_database_stage_has_a_hard_wall_clock_deadline(self):
        for stage in ('CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT','CLOSE'):
            def target(dsn):
                with m.at_stage(stage):time.sleep(60)
            start=time.monotonic();seen=[]
            with self.subTest(stage=stage),self.assertRaisesRegex(m.DiagnosticFailure,stage+':TIMEOUT') as caught:
                m.supervise_check_source(SECRET,total_timeout=2,stage_timeouts={stage:0.1},progress=seen.append,target=target)
            self.assertLess(time.monotonic()-start,2)
            self.assertIn(stage,seen);self.assertNotIn(SECRET,str(caught.exception))

    def test_overall_budget_is_not_reset_by_stage_progress(self):
        def target(dsn):
            while True:
                with m.at_stage('READ_ACCESS'):time.sleep(0.02)
        start=time.monotonic()
        with self.assertRaisesRegex(m.DiagnosticFailure,'TIMEOUT'):
            m.supervise_check_source(SECRET,total_timeout=0.15,stage_timeouts={'READ_ACCESS':1},target=target)
        self.assertLess(time.monotonic()-start,2)

    def test_success_is_small_and_all_five_required_stages_are_observed(self):
        def target(dsn):
            for stage in ('CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT'):
                with m.at_stage(stage):pass
            return RESULT
        seen=[]
        self.assertEqual(m.supervise_check_source(SECRET,total_timeout=2,progress=seen.append,target=target),RESULT)
        for stage in ('CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT'):self.assertIn(stage,seen)

    def test_unrecognized_result_and_raw_exception_are_not_transmitted(self):
        for target in (lambda dsn: {'password':SECRET},lambda dsn: (_ for _ in ()).throw(RuntimeError(SECRET))):
            with self.assertRaisesRegex(m.DiagnosticFailure,'SOURCE_CHECK:UNKNOWN') as caught:
                m.supervise_check_source(SECRET,total_timeout=2,target=target)
            self.assertNotIn(SECRET,str(caught.exception))

    def test_native_descendant_is_terminated_with_the_stalled_child(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'pid'
            def target(dsn):
                with m.at_stage('NATIVE_CONNECT'):
                    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
                    path.write_text(str(child.pid))
                    time.sleep(60)
            with self.assertRaisesRegex(m.DiagnosticFailure,'NATIVE_CONNECT:TIMEOUT'):
                m.supervise_check_source(SECRET,total_timeout=3,stage_timeouts={'NATIVE_CONNECT':0.2},target=target)
            pid=int(path.read_text());stat=Path('/proc')/str(pid)/'stat'
            # The process can disappear between exists() and read_text().
            try:
                state=stat.read_text().split()[2]
            except (FileNotFoundError,ProcessLookupError):
                state=None
            self.assertIn(state,(None,'Z'),'native descendant still active')


class HostDeadlineTests(unittest.TestCase):
    def test_stalled_docker_cli_returns_stage_and_stops_only_created_container(self):
        marker='1'*32;name='grit-source-check-'+marker;calls=[];output=io.StringIO()
        code="import sys,time;print('GRIT_PREFLIGHT_STAGE:CONNECT',file=sys.stderr,flush=True);print('"+SECRET+"',file=sys.stderr,flush=True);sys.stdin.read();time.sleep(60)"
        with contextlib.redirect_stdout(output),self.assertRaisesRegex(kit.Stop,'CONNECT; category TIMEOUT') as caught:
            kit.run_preflight_container([sys.executable,'-c',code],SECRET,name,marker,total_timeout=0.2,stopper=lambda *args:calls.append(args) or True)
        self.assertEqual(calls,[(name,marker)])
        self.assertNotIn(SECRET,str(caught.exception));self.assertNotIn(SECRET,output.getvalue())

    def test_normal_exit_also_confirms_container_stopped_and_does_not_remove(self):
        calls=[];marker='2'*32;name='grit-source-check-'+marker
        result=kit.run_preflight_container([sys.executable,'-c',"import sys;sys.stdin.read();print('safe result')"],SECRET,name,marker,total_timeout=2,stopper=lambda *args:calls.append(args) or True)
        self.assertEqual(result.returncode,0);self.assertEqual(result.stdout,'safe result\n')
        self.assertEqual(calls,[(name,marker)])

    def test_unconfirmed_stop_is_explicit_and_never_relays_docker_errors(self):
        marker='3'*32;name='grit-source-check-'+marker
        with self.assertRaisesRegex(kit.Stop,'stop unconfirmed'):
            kit.run_preflight_container([sys.executable,'-c','pass'],'',name,marker,total_timeout=2,stopper=lambda *args:False)

    def test_scoped_container_stop_checks_label_and_keeps_container(self):
        marker='4'*32;name='grit-source-check-'+marker
        responses=[subprocess.CompletedProcess([],0,stdout=marker+' true\n'),subprocess.CompletedProcess([],0,stdout=''),subprocess.CompletedProcess([],0,stdout=marker+' false\n')]
        with patch.object(kit.subprocess,'run',side_effect=responses) as run:
            self.assertTrue(kit.stop_preflight_container(name,marker))
        calls=[call.args[0] for call in run.call_args_list]
        self.assertTrue(any('stop' in call and name in call for call in calls))
        self.assertFalse(any('rm' in call for call in calls))
        with patch.object(kit.subprocess,'run',return_value=subprocess.CompletedProcess([],0,stdout='other true\n')) as run:
            self.assertFalse(kit.stop_preflight_container(name,marker));self.assertEqual(run.call_count,1)

class StartupAndCancellationTests(unittest.TestCase):
    def test_session_timeouts_exist_before_snapshot_and_connection_is_closed(self):
        from unittest.mock import MagicMock
        conn=MagicMock()
        with patch.object(m,'client_versions',return_value={'pg_dump':17}),patch.object(m,'source_connection',return_value=conn) as connect,patch.object(m,'source_snapshot',side_effect=m.DiagnosticFailure('SNAPSHOT','PERMISSION')):
            with self.assertRaises(m.DiagnosticFailure):m.check_source('postgresql://user:fake@localhost/grit')
        options=m.conninfo_to_dict(connect.call_args.args[0])['options']
        self.assertIn('statement_timeout=15000',options)
        self.assertIn('lock_timeout=5000',options)
        self.assertIn('default_transaction_read_only=on',options)
        conn.close.assert_called_once()

    def test_keyboard_interrupt_stops_owned_container_without_traceback(self):
        marker='5'*32;name='grit-source-check-'+marker;calls=[]
        original_sleep=time.sleep;injected=[]
        def interrupt_once(seconds):
            if not injected:
                injected.append(True);raise KeyboardInterrupt
            return original_sleep(seconds)
        with patch.object(kit.time,'sleep',side_effect=interrupt_once),self.assertRaisesRegex(kit.Stop,'CANCELLED'):
            kit.run_preflight_container([sys.executable,'-c','import time;time.sleep(60)'],SECRET,name,marker,stopper=lambda *args:calls.append(args) or True)
        self.assertEqual(calls,[(name,marker)])

class WorkerCancellationTests(unittest.TestCase):
    def test_worker_sigint_is_sanitized_without_traceback(self):
        import json
        from types import SimpleNamespace
        import input_entrypoint as worker
        payload=json.dumps({'url':'postgresql://user:fake@render.example.invalid/grit','action':'check-source'}).encode()
        with patch.object(worker.sys,'stdin',SimpleNamespace(buffer=io.BytesIO(payload))),patch.object(worker,'supervise_check_source',side_effect=KeyboardInterrupt):
            with self.assertRaises(SystemExit) as caught:worker.main()
        self.assertEqual(str(caught.exception),'GRIT_EXPORT_FAILED:SOURCE_CHECK:CANCELLED')
