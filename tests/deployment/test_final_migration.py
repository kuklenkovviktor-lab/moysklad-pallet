"""Migration safeguards; synthetic credentials only, no remote connections."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'deploy/migration'))
from input_entrypoint import external_dsn
from migration_tool import Refused,conninfo_to_dict
spec=importlib.util.spec_from_file_location('migrate',ROOT/'deploy/migrate.py')
kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)


class FinalMigrationTests(unittest.TestCase):
    def test_external_url_rejects_pasted_shell_multiline_and_unverified_tls(self):
        for value in ('ssh root@example.invalid','postgresql://a:b@host/db\nnext',
                      "'postgresql://a:b@host/db'",'postgresql://a:b@host/db?sslmode=require',
                      'postgresql://a:b@host/db?options=-cBAD','postgresql://a@host/db'):
            with self.subTest(value=value):
                with self.assertRaises(Refused): external_dsn(value)

    def test_external_url_preserves_encoded_password_and_enforces_readonly_tls(self):
        values=conninfo_to_dict(external_dsn('postgresql://user:p%40ss%3A%24@render.example.invalid/db'))
        self.assertEqual(values['password'],'p@ss:$')
        self.assertEqual(values['sslmode'],'verify-full')
        self.assertEqual(values['options'],'-c default_transaction_read_only=on')
        self.assertEqual(values['connect_timeout'],'10')

    def test_noninteractive_secret_entry_is_refused(self):
        with patch.object(sys.stdin,'isatty',return_value=False),patch.object(kit.getpass,'getpass') as prompt:
            with self.assertRaises(kit.Stop):kit.secret('hidden')
            prompt.assert_not_called()

    def test_database_selection_refuses_existing_test_db_and_injection(self):
        for value in ('grit','postgres','restore_check_X','restore_check_x;drop database grit','restore_check_'):
            with self.assertRaises(kit.Stop):kit.database_name(value)
        self.assertEqual(kit.database_name('restore_check_final_20261106'),'restore_check_final_20261106')

    def test_external_secret_only_passed_via_stdin_and_existing_bundle_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'new'
            fake='postgresql://user:fake-only@render.example.invalid/db'
            with patch.object(kit,'secret',return_value=fake),patch.object(kit,'docker') as docker,patch.object(kit,'verify_local'):
                kit.export_external(path)
                args,kwargs=docker.call_args
                self.assertNotIn(fake,str(args))
                self.assertIn(fake,kwargs['input'])
                self.assertNotIn('-e',args)
                self.assertNotIn('--env',args)
            path.mkdir()
            with patch.object(kit,'secret') as prompt:
                with self.assertRaises(kit.Stop):kit.export_external(path)
                prompt.assert_not_called()

    def test_private_configs_and_multiline_values_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            file=Path(temp)/'config';file.write_text('fake');file.chmod(0o644)
            with self.assertRaises(kit.Stop):kit.private_file(file)
            file.chmod(0o600);kit.private_file(file)
            link=Path(temp)/'link';link.symlink_to(file)
            with self.assertRaises(kit.Stop):kit.private_file(link)
        with self.assertRaises(kit.Stop):kit.quote_env('a\nb')

    def test_compose_secret_roundtrip_without_interpolation(self):
        # Exercise the real Compose dotenv parser, not an imitation of it.
        if not __import__('shutil').which('docker'): self.skipTest('Docker Compose unavailable')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            value='fake-$HOME-${PATH}-dollar$$-quote\"-single\'-backslash\\end'
            (root/'secret.env').write_text('VALUE='+kit.quote_env(value)+'\n')
            (root/'compose.yaml').write_text('services:\n  probe:\n    image: postgres:17-bookworm\n    environment:\n      VALUE: ${VALUE}\n')
            env={k:v for k,v in os.environ.items() if k not in (*kit.SELECTORS,'VALUE')}
            result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','compose','--env-file',str(root/'secret.env'),'-f',str(root/'compose.yaml'),'config','--format','json'],env=env,check=True,capture_output=True,text=True)
            self.assertEqual(json.loads(result.stdout)['services']['probe']['environment']['VALUE'],value.replace('$','$$'))
            actual=subprocess.run(['docker','--host=unix:///var/run/docker.sock','compose','--env-file',str(root/'secret.env'),'-f',str(root/'compose.yaml'),'config','--environment'],env=env,check=True,capture_output=True,text=True)
            self.assertEqual(next(line[6:] for line in actual.stdout.splitlines() if line.startswith('VALUE=')),value)

    def test_export_failure_does_not_print_payload(self):
        with patch.object(kit.subprocess,'run',side_effect=subprocess.CalledProcessError(1,['docker'],stderr='fake-secret')):
            with self.assertRaisesRegex(kit.Stop,'Command failed') as error:kit.run(['docker'],capture_output=True)
            self.assertNotIn('fake-secret',str(error.exception))

class PrintAgentMigrationTests(unittest.TestCase):
    def test_hostkey_is_explicit_and_default_still_render(self):
        import onix_print_agent as agent
        with patch.dict(os.environ,{'ONIX_PRINT_AGENT_TOKEN':'synthetic-only-token-at-least-32-characters'}),patch.object(agent,'run_once') as run:
            self.assertEqual(agent.main(['--once','--server','https://gritwms.space']),0)
            self.assertEqual(run.call_args.args[0],'https://gritwms.space')
            self.assertEqual(agent.main(['--once']),0)
            self.assertEqual(run.call_args.args[0],agent.DEFAULT_SERVER)
        with patch.object(agent,'run_once') as run,patch('sys.stderr'):
            with self.assertRaises(SystemExit):agent.main(['--once','--server','https://untrusted.example.invalid'])
            run.assert_not_called()

class OriginalFileMigrationTests(unittest.TestCase):
    def test_file_archive_preserves_bytes_and_rejects_overwrite_and_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'originals';source.mkdir()
            (source/'WB.pdf').write_bytes(b'fake PDF binary\x00\xff')
            (source/'ONIX_WB_REVIEW.json').write_text('{"status":"submitted"}')
            destination=root/'bundle'
            kit.pack_files(source,destination)
            kit.verify_files(destination)
            self.assertEqual((source/'WB.pdf').read_bytes(),b'fake PDF binary\x00\xff')
            with self.assertRaises(FileExistsError):kit.pack_files(source,destination)
            with (destination/'files.tar.gz').open('ab') as stream:stream.write(b'bad')
            with self.assertRaises(kit.Stop):kit.verify_files(destination)

    def test_symlink_and_recursive_destination_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'source';source.mkdir();(source/'document').write_text('fake')
            with self.assertRaises(kit.Stop):kit.pack_files(source,source/'recursive')
            (source/'link').symlink_to(source/'document')
            with self.assertRaises(kit.Stop):kit.pack_files(source,root/'bundle')
            self.assertFalse((root/'bundle').exists())

class CutoverGuardTests(unittest.TestCase):
    def test_live_start_requires_external_receipt_matching_every_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            (root/'database.dump').write_bytes(b'fake')
            (root/'manifest.json').write_text('{}')
            checks={name:kit.sha(root/name) for name in ('database.dump','manifest.json')}
            (root/'checksums.json').write_text(json.dumps(checks))
            with self.assertRaises(kit.Stop):kit.check_external_receipt(root)
            checks['checksums.json']=kit.sha(root/'checksums.json')
            receipt={'objects':[{'key':'fake/'+name,'sha256':value} for name,value in checks.items()]}
            (root/'external-copy-fake.json').write_text(json.dumps(receipt))
            kit.check_external_receipt(root)
            (root/'checksums.json').write_text('{}')
            with self.assertRaises(kit.Stop):kit.check_external_receipt(root)

    def test_failed_live_start_stops_target_writers_without_touching_db(self):
        from types import SimpleNamespace
        name='restore_check_fixture'
        calls=[]
        def compose(*args,**kwargs):
            calls.append((args,kwargs))
            if args[0]=='config':return SimpleNamespace(stdout=json.dumps({'services':{'app':{'environment':{'GRIT_DATABASE_NAME':name}},'backup':{'environment':{'BACKUP_S3_ENABLED':'1','BACKUP_ALERT_REQUIRED':'1'}}}}))
            if args[0]=='up':raise kit.Stop('Synthetic startup failure')
            return SimpleNamespace(stdout='')
        with tempfile.TemporaryDirectory() as temp,patch.object(kit,'preflight'),patch.object(kit,'private_file'),patch.object(kit,'operations'),patch.object(kit,'check_external_receipt'),patch.object(kit,'compose',side_effect=compose),patch('builtins.input',return_value='RENDER WRITERS STOPPED'):
            with self.assertRaises(kit.Stop):kit.staged_start(temp,name,live=True)
        self.assertEqual(calls[-1][0],('stop','app','backup','caddy'))
        self.assertFalse(any('db' in args for args,_ in calls))

    def test_update_refuses_selected_live_database_before_build_or_prompt(self):
        from types import SimpleNamespace
        with patch.object(kit,'preflight',return_value=('db',Path('/tmp/fake'))),patch.object(kit,'compose',return_value=SimpleNamespace(stdout='app')),patch.object(kit,'docker',return_value=SimpleNamespace(stdout=json.dumps([{'Config':{'Env':['GRIT_DATABASE_NAME=restore_check_live']}}]))),patch('builtins.input') as prompt:
            with self.assertRaises(kit.Stop):kit.update()
            prompt.assert_not_called()

class RepositoryDataGuardTests(unittest.TestCase):
    def test_export_refuses_git_workspace_before_requesting_credentials(self):
        with patch.object(kit,'secret') as prompt:
            with self.assertRaisesRegex(kit.Stop,'outside the Git'):kit.export_external(ROOT/'never-created-secret-bundle')
            prompt.assert_not_called()
        self.assertFalse((ROOT/'never-created-secret-bundle').exists())

class LiveBackupOrderTests(unittest.TestCase):
    def test_notification_and_fresh_live_backup_precede_integration_start(self):
        from types import SimpleNamespace
        name='restore_check_fixture';calls=[]
        def compose(*args,**kwargs):
            calls.append((args,kwargs))
            if args[0]=='config':return SimpleNamespace(stdout=json.dumps({'services':{'app':{'environment':{'GRIT_DATABASE_NAME':name}},'backup':{'environment':{'BACKUP_S3_ENABLED':'1','BACKUP_ALERT_REQUIRED':'1'}}}}))
            return SimpleNamespace(stdout='')
        with tempfile.TemporaryDirectory() as temp,patch.object(kit,'preflight'),patch.object(kit,'private_file'),patch.object(kit,'operations'),patch.object(kit,'check_external_receipt'),patch.object(kit,'compose',side_effect=compose),patch('builtins.input',return_value='RENDER WRITERS STOPPED'):
            kit.staged_start(temp,name,live=True)
        notify=next(i for i,(args,_) in enumerate(calls) if 'notify-test' in args)
        backup=next(i for i,(args,_) in enumerate(calls) if '/scripts/backup-once.sh' in args)
        start=next(i for i,(args,_) in enumerate(calls) if args[0]=='up' and 'app' in args)
        health=next(i for i,(args,_) in enumerate(calls) if '/scripts/backup-health.sh' in args)
        self.assertLess(notify,backup);self.assertLess(backup,health);self.assertLess(health,start)
        self.assertTrue(calls[backup][1]['live'])

class ProductionOverlayTests(unittest.TestCase):
    def test_combined_restore_live_https_config_preserves_db_and_hides_app_port(self):
        if not __import__('shutil').which('docker'):self.skipTest('Compose unavailable')
        import shutil
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            for name in ('compose.yaml','compose.ops.yaml','compose.restore.yaml','compose.live.yaml','compose.https.yaml'):
                shutil.copyfile(ROOT/name,root/name)
            values={key:'synthetic-only' for key in (*kit.SECRETS,'POSTGRES_PASSWORD')}
            values.update(GRIT_DATA_ROOT=str(root/'data'),BACKUP_S3_ENABLED='1',BACKUP_ALERT_REQUIRED='1')
            (root/'.env').write_text(''.join(key+'='+kit.quote_env(value)+'\n' for key,value in values.items()));(root/'.env').chmod(0o600)
            (root/'.env.cutover').write_text('GRIT_DATABASE_NAME=restore_check_final\nMOYSKLAD_TOKEN=synthetic-only\nGRIT_ACME_EMAIL=test@example.invalid\n');(root/'.env.cutover').chmod(0o600)
            with patch.object(kit,'ROOT',root):
                result=kit.compose('config','--format','json',live=True,https=True,capture_output=True,text=True)
            config=json.loads(result.stdout)['services']
            self.assertEqual(config['db']['environment']['POSTGRES_DB'],'grit')
            self.assertEqual(config['app']['environment']['GRIT_DATABASE_NAME'],'restore_check_final')
            self.assertEqual(config['backup']['environment']['PGDATABASE'],'restore_check_final')
            self.assertEqual(config['app']['environment']['MOYSKLAD_TOKEN'],'synthetic-only')
            self.assertFalse(config['app'].get('ports'))
            self.assertFalse(config['db'].get('ports'))
            self.assertEqual({p['published'] for p in config['caddy']['ports']},{'80','443'})
