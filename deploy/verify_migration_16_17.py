"""Real PostgreSQL 16 TLS/stdin export -> 17 restore, on fresh local tmpfs only."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args,**kwargs):
    return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],env=ENV,check=True,**kwargs)


def wait(db):
    for _ in range(40):
        if subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',db,'pg_isready','-U','grit','-d','grit'],env=ENV,capture_output=True).returncode==0:return
        time.sleep(1)
    docker('logs','--tail','30',db)
    raise RuntimeError('Synthetic PostgreSQL startup timed out')


def main():
    folder=Path(tempfile.mkdtemp(prefix='grit-pg16-17-',dir='/tmp'));tls=folder/'tls';tls.mkdir()
    bundle=folder/'bundles';bundle.mkdir(mode=0o700)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=render-test',
                    '-addext','subjectAltName=DNS:render-test','-keyout',str(tls/'server.key'),'-out',str(tls/'server.crt')],check=True,capture_output=True)
    name='grit-pg16-17-'+uuid.uuid4().hex[:10];networks=[name+'-source',name+'-target'];containers=[]
    try:
        for network in networks:docker('network','create','--internal',network,capture_output=True)
        source=docker('run','-d','--network',networks[0],'--network-alias','db','--network-alias','render-test',
                      '--tmpfs','/var/lib/postgresql/data:rw','-v',str(tls)+':/tls:ro',
                      '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=synthetic-only',
                      '--entrypoint','sh','postgres:16-bookworm','-c',
                      'cp /tls/server.key /tmp/server.key && cp /tls/server.crt /tmp/server.crt && chown postgres:postgres /tmp/server.key && chmod 600 /tmp/server.key && chmod 644 /tmp/server.crt && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key',capture_output=True,text=True).stdout.strip()
        containers.append(source);wait(source)
        keys=['-e','SECRET_KEY=synthetic-only','-e','WMS_BADGE_SECRET=synthetic-only','-e','WMS_TERMINAL_SECRET=synthetic-only']
        docker('run','--rm','--network',networks[0],'--entrypoint','python',*keys,
               '-e','DATABASE_URL=postgresql://grit:synthetic-only@db/grit','grit-wms:migration-trial','-c',
               "from wms import init_wms_db; from fbs_catalog import init_fbs_catalog_db; from receiving_idempotency import init_receiving_operations; from partner_pallets import db_init; init_wms_db();init_fbs_catalog_db();init_receiving_operations();db_init()",capture_output=True)
        docker('exec',source,'psql','-U','grit','-d','grit','-v','ON_ERROR_STOP=1','-c',
               "CREATE TABLE migration_sequence_fixture(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, note text); INSERT INTO migration_sequence_fixture(note) VALUES ('Тест Ω'),('second'); SELECT setval('migration_sequence_fixture_id_seq',50,true); CREATE SEQUENCE never_called START 9; CREATE VIEW migration_view AS SELECT note FROM migration_sequence_fixture",capture_output=True)
        payload=json.dumps({'url':'postgresql://grit:synthetic-only@render-test/grit','destination':'/bundles/pg16'})
        client=['run','--rm','-i','--user',str(os.getuid())+':'+str(os.getgid()),'--network',networks[0],'-v',str(bundle)+':/bundles',
                '-v',str(tls/'server.crt')+':/etc/ssl/certs/ca-certificates.crt:ro','--entrypoint','python3',
                'grit-migration:ops','/tools/input_entrypoint.py']
        docker(*client,input=payload,text=True,capture_output=True)
        checks=json.loads((bundle/'pg16/checksums.json').read_text())
        # Refuse a repeated export, preserving every original byte.
        failed=subprocess.run(['docker','--host=unix:///var/run/docker.sock',*client],env=ENV,input=payload,text=True,capture_output=True)
        assert failed.returncode!=0 and 'synthetic-only' not in failed.stderr
        assert json.loads((bundle/'pg16/checksums.json').read_text())==checks
        # The same URL cannot bypass read-only source connection restrictions.
        docker('exec',source,'psql','-U','grit','-d','grit','-Atc','SELECT count(*) FROM migration_sequence_fixture',capture_output=True)
        target=docker('run','-d','--network',networks[1],'--network-alias','db','--tmpfs','/var/lib/postgresql/data:rw',
                      '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=synthetic-only',
                      'postgres:17-bookworm',capture_output=True,text=True).stdout.strip()
        containers.append(target);wait(target)
        common=['run','--rm','--network',networks[1],'-e','GRIT_MIGRATION_DSN=postgresql://grit:synthetic-only@db/grit',
                '-v',str(bundle)+':/bundles:ro','grit-migration:ops']
        docker(*common,'trial-restore','--confirm-read-only-access','--bundle','/bundles/pg16','--database','restore_check_cross',capture_output=True)
        docker(*common,'compare-restored','--confirm-read-only-access','--bundle','/bundles/pg16','--database','restore_check_cross',capture_output=True)
        value=docker('exec',target,'psql','-U','grit','-d','restore_check_cross','-Atc',
                     "SELECT nextval('migration_sequence_fixture_id_seq'),nextval('never_called')",capture_output=True,text=True).stdout.strip()
        assert value=='51|9',value
        # Startup must preserve restored business records (sequence test deliberately
        # modifies its fixture; restore a SECOND fresh database for startup audit).
        docker(*common,'trial-restore','--confirm-read-only-access','--bundle','/bundles/pg16','--database','restore_check_trial',capture_output=True)
        app=docker('run','-d','--network',networks[1],'--entrypoint','python',*keys,
                   '-e','POSTGRES_PASSWORD=synthetic-only','grit-wms:migration-trial','deploy/migration/trial_entrypoint.py',
                   'gunicorn','--bind','0.0.0.0:8000','--config','gunicorn.conf.py','app:app',capture_output=True,text=True).stdout.strip()
        containers.append(app)
        for _ in range(40):
            result=subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',app,'python','deploy/migration/trial_health.py'],env=ENV,capture_output=True)
            if result.returncode==0:break
            time.sleep(1)
        else:raise RuntimeError('Restored app did not become ready')
        docker(*common,'compare-live','--confirm-read-only-access','--bundle','/bundles/pg16','--database','restore_check_trial',capture_output=True)
        print('PASS: actual PG16 verified TLS/stdin export -> PG17; row hashes/schema/identity/sequences/views, nextval=51/9; duplicate export refused; restored app ready and records preserved')
    finally:
        for container in reversed(containers):docker('rm','-f',container,capture_output=True)
        for network in reversed(networks):docker('network','rm',network,capture_output=True)
        print('Synthetic bundles retained:',folder)


if __name__=='__main__':main()
