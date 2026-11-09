"""Local-only PG16 TLS -> PG17 and timeout rehearsal. Keep ALL new containers/data.

Three unique containers are reused with docker exec to limit disk consumption.
No rm/down/prune, external URLs, existing paths or real credentials are accepted.
"""
import importlib.util
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
    return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],env=ENV,check=True,timeout=120,**kwargs)


def wait(name):
    for _ in range(40):
        if subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',name,'pg_isready','-U','grit','-d','grit'],env=ENV,capture_output=True,timeout=5).returncode==0:return
        time.sleep(1)
    raise RuntimeError('Synthetic database did not start')


def main():
    marker=uuid.uuid4().hex;network='grit-deadline-'+marker
    folder=Path(tempfile.mkdtemp(prefix='grit-deadline-',dir='/tmp'))
    for name in ('tls','bundles','source','target'):(folder/name).mkdir(mode=0o700)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=render-test',
                    '-addext','subjectAltName=DNS:render-test','-keyout',str(folder/'tls/server.key'),'-out',str(folder/'tls/server.crt')],check=True,capture_output=True)
    docker('network','create','--internal',network,capture_output=True)
    source=network+'-pg16';target=network+'-pg17';tool='grit-source-check-'+marker;created=[]
    try:
        docker('run','-d','--name',source,'--network',network,'--network-alias','render-test',
               '-v',str(folder/'source')+':/var/lib/postgresql/data','-v',str(folder/'tls')+':/tls:ro',
               '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=synthetic-only',
               '--entrypoint','sh','postgres:16-bookworm','-c',
               'cp /tls/server.key /tmp/server.key && cp /tls/server.crt /tmp/server.crt && chown postgres:postgres /tmp/server.key && chmod 600 /tmp/server.key && chmod 644 /tmp/server.crt && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key',capture_output=True)
        created.append(source);wait(source)
        schema="""CREATE TABLE employees(id text PRIMARY KEY,name text);
CREATE TABLE shifts(id text PRIMARY KEY,employee_id text REFERENCES employees,ended_at text);
CREATE TABLE activities(id text PRIMARY KEY,shift_id text REFERENCES shifts,employee_id text,ended_at text);
CREATE TABLE scans(id text PRIMARY KEY,shift_id text REFERENCES shifts,employee_id text,barcode text,product_barcode text);
CREATE TABLE operation_events(id text PRIMARY KEY,employee_id text,shift_id text,quantity int);
CREATE TABLE serial_probe(id serial PRIMARY KEY,note text);
INSERT INTO employees VALUES ('e1','Тестовый сотрудник');
INSERT INTO shifts VALUES ('s1','e1',NULL);
INSERT INTO activities VALUES ('a1','s1','e1',NULL);
INSERT INTO scans VALUES ('scan1','s1','e1','WB-fake','SKU-fake');
INSERT INTO operation_events VALUES ('ev1','e1','s1',1);
INSERT INTO serial_probe(note) VALUES ('Ω'); SELECT setval('serial_probe_id_seq',41,true);"""
        docker('exec','-i',source,'psql','-U','grit','-d','grit','-v','ON_ERROR_STOP=1',input=schema,text=True,capture_output=True)
        docker('run','-d','--name',target,'--network',network,'--network-alias','db',
               '-v',str(folder/'target')+':/var/lib/postgresql/data',
               '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=synthetic-only','postgres:17-bookworm',capture_output=True)
        created.append(target);wait(target)
        docker('run','-d','--name',tool,'--label','grit.migration.preflight='+marker,'--network',network,
               '--user',str(os.getuid())+':'+str(os.getgid()),'--read-only','--tmpfs','/tmp','--cap-drop=ALL',
               '-v',str(folder/'bundles')+':/bundles','-v',str(folder/'tls/server.crt')+':/etc/ssl/certs/ca-certificates.crt:ro',
               '--entrypoint','python3','grit-migration:ops','-c','import time;time.sleep(600)',capture_output=True)
        created.append(tool)
        url='postgresql://grit:synthetic-only@render-test/grit'
        def worker(action,**extra):
            return docker('exec','-i',tool,'python3','/tools/input_entrypoint.py',input=json.dumps({'url':url,'action':action,**extra}),text=True,capture_output=True)
        check=worker('check-source')
        for stage in ('CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT'):
            assert 'GRIT_PREFLIGHT_STAGE:'+stage in check.stderr
        assert not list((folder/'bundles').iterdir())
        assert 'synthetic-only' not in check.stderr and url not in check.stdout
        # A genuine waiting SQL query is bounded by the independent supervisor.
        probe="""import sys,time
import migration_tool as m
from input_entrypoint import external_dsn
original=m.source_snapshot
def stalled(conn,major):
    with m.at_stage('SNAPSHOT'):
        conn.execute("SET statement_timeout='1500ms'")
        conn.execute('SELECT pg_sleep(30)')
m.source_snapshot=stalled
started=time.monotonic()
try:m.supervise_check_source(external_dsn(sys.stdin.read()),total_timeout=3,stage_timeouts={'SNAPSHOT':0.2})
except m.DiagnosticFailure as error:
    assert error.stage=='SNAPSHOT' and error.category=='TIMEOUT'
    assert time.monotonic()-started<3
    print('PASS: blocked SQL returned SNAPSHOT:TIMEOUT')
else:raise AssertionError('stalled query was accepted')
"""
        result=docker('exec','-i',tool,'python3','-c',probe,input=url,text=True,capture_output=True)
        print(result.stdout.strip())
        worker('export',destination='/bundles/full')
        restore="""import migration_tool as m
from input_entrypoint import external_dsn
m.trial_restore('postgresql://grit:synthetic-only@db/grit','/bundles/full','restore_check_deadline')
expected=m.verify_bundle('/bundles/full')
assert m.compare(expected,m.snapshot(external_dsn('postgresql://grit:synthetic-only@render-test/grit')))==[]
print('PASS: PG16 verified TLS export -> PG17 restore; all row hashes, constraints, sequences match; source unchanged')
"""
        print(docker('exec','-i',tool,'python3','-c',restore,text=True,capture_output=True).stdout.strip())
        value=docker('exec',target,'psql','-U','grit','-d','restore_check_deadline','-Atc',"SELECT nextval('serial_probe_id_seq')",capture_output=True,text=True).stdout.strip()
        assert value=='42'
        # Real host reader/stop implementation, using the same NEW labeled tool
        # container, not allocating another image copy. Its data stays retained.
        spec=importlib.util.spec_from_file_location('deadline_kit',ROOT/'deploy/migrate.py');kit=importlib.util.module_from_spec(spec);spec.loader.exec_module(kit)
        result=kit.run_preflight_container(['docker','--host=unix:///var/run/docker.sock','exec','-i',tool,'python3','/tools/input_entrypoint.py'],
                    json.dumps({'url':url,'action':'check-source'}),tool,marker,total_timeout=30)
        assert result.returncode==0 and 'Source check passed:' in result.stdout
        assert docker('inspect','--format','{{.State.Running}}',tool,text=True,capture_output=True).stdout.strip()=='false'
        print('PASS: live host stage stream, successful completion and scoped Docker stop; tool container retained')
    finally:
        for name in reversed(created):
            try:docker('stop','--time','2',name,capture_output=True)
            except subprocess.SubprocessError:pass
        print('All created containers/network/database bind files/bundles RETAINED:',network,folder)


if __name__=='__main__':main()
