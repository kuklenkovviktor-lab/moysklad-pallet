"""Rehearse a manual update on NEW local bind directories; never contacts HOSTKEY.

Preserves the running PostgreSQL container, secret files, existing archive and
all baseline business records. Only our fresh fixture loses its empty request
table to simulate the pre-PR93 schema. No production DSN/path is accepted.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

from init_env import generate

ROOT=Path(__file__).resolve().parents[1]


def main():
    folder=Path(tempfile.mkdtemp(prefix='grit-rollout-',dir='/tmp'))
    for name in ('postgres','backups','operations'): (folder/name).mkdir()
    generate(folder/'.env')
    private=folder/'.env.backup';private.write_text('BACKUP_S3_ACCESS_KEY_ID=isolated-test-only\n');private.chmod(0o600)
    secret_before={(folder/name).name:(folder/name).read_bytes() for name in ('.env','.env.backup')}
    project='grit-rollout-'+uuid.uuid4().hex[:10]
    environment={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','POSTGRES_PASSWORD','SECRET_KEY','WMS_BADGE_SECRET','WMS_TERMINAL_SECRET','MANAGER_WEB_PASSWORD')}
    environment.update(GRIT_DATA_ROOT=str(folder),GRIT_OPERATIONS_DIR=str(folder/'operations'),GRIT_PORT='0',BACKUP_S3_ENABLED='0',BACKUP_ALERT_REQUIRED='0')
    # The local repository's optional credentials must never be inherited.
    fast=folder/'test.yaml'
    fast.write_text('services:\n' + ''.join(
        '  '+name+':\n    healthcheck:\n      interval: 2s\n      start_period: 10s\n      retries: 15\n'
        for name in ('app','db')) +
        '  backup:\n    healthcheck:\n      interval: 2s\n      start_period: 10s\n      retries: 15\n'
        '    env_file: !override\n      - path: '+str(private)+'\n        required: true\n'
        '    environment:\n      BACKUP_S3_ACCESS_KEY_ID: isolated-test-only\n'
        '      BACKUP_S3_SECRET_ACCESS_KEY: isolated-test-only\n'
        '      BACKUP_ALERT_WEBHOOK_URL: ""\n      BACKUP_ALERT_TELEGRAM_TOKEN: ""\n'
        '      BACKUP_ALERT_TELEGRAM_CHAT_ID: ""\n')
    compose=['docker','--host=unix:///var/run/docker.sock','compose','--project-directory',str(ROOT),'--env-file',str(folder/'.env'),'-p',project,'-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.ops.yaml'),'-f',str(fast)]

    def docker(*args,**kwargs):
        return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],env=environment,check=True,text=True,**kwargs)

    def dc(*args,**kwargs):
        return subprocess.run(compose+list(args),env=environment,check=True,text=True,**kwargs)

    def ops(*args):
        return dc('run','--rm','--no-deps','ops',*args)

    def query(statement):
        return dc('exec','-T','db','psql','-v','ON_ERROR_STOP=1','-U','grit','-d','grit','-Atc',statement,capture_output=True).stdout.strip()

    try:
        docker('run','--rm','--network','none','-v',str(folder/'backups')+':/backups','-v',str(folder/'operations')+':/operations','postgres:17-bookworm','sh','-c',
               'chown 0:0 /backups /operations && chmod 700 /backups /operations && printf isolated-existing-archive > /backups/keep-existing.archive')
        dc('config','--quiet')
        dc('up','-d','--no-build','--wait','--wait-timeout','180','app','db','backup')
        before=dc('ps','-q','db',capture_output=True).stdout.strip()
        dc('stop','app','backup')
        # Existing business/configuration data must survive startup unmodified.
        query("UPDATE employees SET active=0,rate=9,target=77,name='Preserve fixture' WHERE id='anton'")
        query("INSERT INTO shifts(id,employee_id,started_at) VALUES ('rollout-shift','ilya','2026-11-06T00:00:00+00:00')")
        query("INSERT INTO activities(id,shift_id,employee_id,activity_type,started_at) VALUES ('rollout-activity','rollout-shift','ilya','picking','2026-11-06T00:00:00+00:00')")
        query("INSERT INTO fbs_pending_products(shift_id,employee_id,product_barcode,article,product_name,started_at) VALUES ('rollout-shift','ilya','2038719091772','fixture','Fixture','2026-11-06T00:00:00+00:00')")
        # Only our newly generated DB, empty ledger: model the old release.
        assert query('SELECT count(*) FROM fbs_scan_requests')=='0'
        query('DROP TABLE fbs_scan_requests')
        ops('export','--confirm-read-only-access','--bundle','/operations/baseline')
        ops('verify','--bundle','/operations/baseline')
        ops('trial-restore','--confirm-read-only-access','--bundle','/operations/baseline','--database','restore_check_rollout')
        dc('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','180','app')
        assert dc('ps','-q','db',capture_output=True).stdout.strip()==before
        ops('compare-live','--confirm-read-only-access','--bundle','/operations/baseline')
        dc('run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-once.sh')
        dc('up','-d','--no-build','--no-deps','--wait','--wait-timeout','180','backup')
        assert dc('ps','-q','db',capture_output=True).stdout.strip()==before
        assert query("SELECT active||':'||rate||':'||target||':'||name FROM employees WHERE id='anton'")=='0:9:77:Preserve fixture'
        assert query("SELECT product_barcode FROM fbs_pending_products WHERE shift_id='rollout-shift'")=='2038719091772'
        dc('exec','-T','backup','python3','-c',"from pathlib import Path; assert Path('/backups/keep-existing.archive').read_bytes()==b'isolated-existing-archive'")
        for name,content in secret_before.items(): assert (folder/name).read_bytes()==content
        ops('verify','--bundle','/operations/baseline')
        print('PASS: application-only rollout preserves DB container, secrets, existing archive, disabled employee settings, active shift/pending and all baseline record hashes; only approved empty ledger added')
    finally:
        dc('down','--remove-orphans')
        print('Synthetic rollout files retained at',folder)


if __name__=='__main__': main()
