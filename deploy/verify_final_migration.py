"""Exercise the actual interactive update/trial commands against a NEW local stack.

Copies repository into /tmp, uses only fake keys and fresh PostgreSQL bind data.
Never takes a remote DSN or an existing host path as input.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from unittest.mock import patch

from init_env import generate

ROOT=Path(__file__).resolve().parents[1]


def main():
    folder=Path(tempfile.mkdtemp(prefix='grit-final-kit-',dir='/tmp'));repo=folder/'repo'
    shutil.copytree(ROOT,repo,ignore=shutil.ignore_patterns('.git','.env','.env.*','__pycache__','.pytest_cache','*.db','*.dump','*.partial'))
    data=folder/'data';data.mkdir();(data/'backups').mkdir();(data/'postgres').mkdir()
    generate(repo/'.env')
    project='grit-final-'+uuid.uuid4().hex[:10]
    with (repo/'.env').open('a') as stream:
        stream.write('GRIT_DATA_ROOT='+str(data)+'\nGRIT_PORT=0\nCOMPOSE_PROJECT_NAME='+project+'\n')
    (repo/'.env.backup').write_text('BACKUP_S3_ACCESS_KEY_ID=fake-only\nBACKUP_S3_SECRET_ACCESS_KEY=fake-only\n');(repo/'.env.backup').chmod(0o600)
    spec=importlib.util.spec_from_file_location('final_kit',repo/'deploy/migrate.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    try:
        m.docker('run','--rm','--network','none','-v',str(data/'backups')+':/backups','postgres:17-bookworm','sh','-c',
                 'chown 0:0 /backups && chmod 700 /backups && printf keep-existing > /backups/existing.archive',capture_output=True)
        m.run(['docker','--host=unix:///var/run/docker.sock','compose','--project-directory',str(repo),'--env-file',str(repo/'.env'),'-f',str(repo/'compose.yaml'),'up','-d','--no-build','--wait','--wait-timeout','180','db','app','backup'],capture_output=True)
        original_db=m.preflight()[0]
        def query(statement,database='grit'):
            return m.compose('exec','-T','db','psql','-U','grit','-d',database,'-v','ON_ERROR_STOP=1','-Atc',statement,capture_output=True,text=True).stdout.strip()
        query("UPDATE employees SET active=0,rate=9,target=77,name='Preserve fixture' WHERE id='anton'")
        query("CREATE TABLE final_kit_probe(id int PRIMARY KEY, value text NOT NULL);INSERT INTO final_kit_probe VALUES (1,'keep all data')")
        before={name:m.sha(repo/name) for name in ('.env','.env.backup')}
        with patch.object(m,'input',return_value='UPDATE TEST APP',create=True):m.update()
        assert m.preflight()[0]==original_db
        assert before=={name:m.sha(repo/name) for name in before}
        assert query("SELECT active||':'||rate||':'||target||':'||name FROM employees WHERE id='anton'")=='0:9:77:Preserve fixture'
        bundle=next((data/'migration').glob('update-*/baseline'))
        m.operations('trial-restore',bundle,'restore_check_final_kit')
        (repo/'.env.cutover').write_text('GRIT_DATABASE_NAME=restore_check_final_kit\n');(repo/'.env.cutover').chmod(0o600)
        m.staged_start(str(bundle),'restore_check_final_kit')
        assert query('SELECT value FROM final_kit_probe','restore_check_final_kit')=='keep all data'
        assert query('SELECT value FROM final_kit_probe')=='keep all data'
        selected=json.loads(m.compose('config','--format','json',live=True,capture_output=True,text=True).stdout)
        assert selected['services']['backup']['environment']['PGDATABASE']=='restore_check_final_kit'
        assert selected['services']['app']['environment']['MOYSKLAD_TOKEN']==''
        assert selected['services']['app']['environment']['WB_API_TOKEN']==''
        m.compose('run','--rm','--no-deps','--entrypoint','sh','backup','-c','test "$(cat /backups/existing.archive)" = keep-existing',capture_output=True)
        assert m.preflight()[0]==original_db
        try:m.update()
        except m.Stop:pass
        else:raise AssertionError('Test-only updater accepted the restored/live app')
        m.stop_local()
        assert m.preflight()[0]==original_db
        print('PASS: ACTUAL update and trial-app commands; existing db container/data, secret files and prior backup preserved; separate restored database selected; integrations blank; backup targets selected DB')
    finally:
        # Only this unique synthetic stack is removed; bind data retained.
        m.compose('down',capture_output=True)
        print('Synthetic artifacts retained:',folder)


if __name__=='__main__':main()
