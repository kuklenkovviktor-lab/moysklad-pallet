"""Run migration tests ONLY against a new local Docker PostgreSQL with tmpfs."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args, **kwargs):
    return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],env=ENV,check=True,text=True,**kwargs)


def main():
    name='grit-migration-'+uuid.uuid4().hex[:10];db=None;app=None
    bundles=Path(tempfile.mkdtemp(prefix='grit-migration-bundles-',dir='/tmp'))
    docker('network','create','--internal',name,capture_output=True)
    try:
        db=docker('run','-d','--network',name,'--network-alias','db','--tmpfs','/var/lib/postgresql/data:rw',
                  '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=isolated-test-only',
                  'postgres:17-bookworm',capture_output=True).stdout.strip()
        for _ in range(30):
            probe=subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',db,'pg_isready','-U','grit','-d','grit'],env=ENV,capture_output=True)
            if probe.returncode==0: break
            time.sleep(1)
        else: raise RuntimeError('Disposable PostgreSQL did not start')
        docker('run','--rm','--network',name,'--entrypoint','python3','-v',str(ROOT/'tests')+':/test:ro',
               '-e','GRIT_MIGRATION_TEST_ADMIN_DSN=postgresql://grit:isolated-test-only@db:5432/grit',
               'grit-migration:test','-m','unittest','discover','-s','/test','-p','test_migration_postgres.py','-v')
        # A full GRIT schema, followed by export -> restore -> actual app startup.
        keys=['-e','WMS_BADGE_SECRET=trial-only','-e','WMS_TERMINAL_SECRET=trial-only','-e','SECRET_KEY=trial-only']
        dsn='postgresql://grit:isolated-test-only@db:5432/grit'
        docker('run','--rm','--network',name,'--entrypoint','python','-e','DATABASE_URL='+dsn,
               *keys,'grit-wms:migration-trial','-c',
               'from wms import init_wms_db; from fbs_catalog import init_fbs_catalog_db; from receiving_idempotency import init_receiving_operations; init_wms_db();init_fbs_catalog_db();init_receiving_operations()')
        # Only the freshly created artificial DB: simulate the pre-PR93 schema.
        docker('exec',db,'psql','-v','ON_ERROR_STOP=1','-U','grit','-d','grit','-c','DROP TABLE IF EXISTS fbs_scan_requests')
        docker('run','--rm','--network',name,'-e','GRIT_MIGRATION_DSN='+dsn,
               '-v',str(bundles)+':/bundles','grit-migration:test','export',
               '--confirm-read-only-access','--bundle','/bundles/full-grit')
        docker('run','--rm','--network',name,'-e','GRIT_MIGRATION_DSN='+dsn,
               '-v',str(bundles)+':/bundles:ro','grit-migration:test','trial-restore',
               '--confirm-read-only-access','--bundle','/bundles/full-grit','--database','restore_check_trial')
        app=docker('run','-d','--network',name,'--entrypoint','python',
                   '-e','POSTGRES_PASSWORD=isolated-test-only',*keys,
                   'grit-wms:migration-trial','deploy/migration/trial_entrypoint.py',
                   'gunicorn','--bind','0.0.0.0:8000','--config','gunicorn.conf.py','app:app',capture_output=True).stdout.strip()
        for _ in range(30):
            probe=subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',app,'python','deploy/migration/trial_health.py'],env=ENV,capture_output=True)
            if probe.returncode==0: break
            time.sleep(1)
        else: raise RuntimeError('Restored GRIT trial application did not become ready')
        trial_dsn='postgresql://grit:isolated-test-only@db:5432/restore_check_trial'
        docker('run','--rm','--network',name,'-e','GRIT_MIGRATION_DSN='+trial_dsn,
               '-v',str(bundles)+':/bundles:ro','grit-migration:test','compare-live',
               '--confirm-read-only-access','--bundle','/bundles/full-grit')
        print('PASS: pre-PR93 GRIT snapshot/export/restore, restored app readiness and post-startup preservation check')
    finally:
        if app: docker('rm','-f',app,capture_output=True)
        print('Synthetic migration bundles retained at',bundles)
        if db: docker('rm','-f',db,capture_output=True)
        docker('network','rm',name,capture_output=True)


if __name__=='__main__': main()
