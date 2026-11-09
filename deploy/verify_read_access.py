"""Real local PostgreSQL lock/slow-query tests. No production access or removals.

Default creates new synthetic PG16/tool fixtures. --reuse-fixture accepts only
our UUID-labeled internal-network fixture from verify_preflight_deadlines.py.
Current module source is sent over stdin: useful when Docker build storage is
full, and also ensures this test runs the exact checked-out Python code.
"""
import argparse
import json
import os
from pathlib import Path
import selectors
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
DOCKER=['docker','--host=unix:///var/run/docker.sock']


def docker(*args,**kwargs):
    return subprocess.run(DOCKER+list(args),env=ENV,check=True,timeout=30,**kwargs)


BOOTSTRAP=r'''
import hashlib,json,sys,time
import migration_tool as m
p=json.load(sys.stdin)
exec(compile(p['module'],m.__file__,'exec'),m.__dict__)
from input_entrypoint import external_dsn
dsn=external_dsn(p['url']);scenario=p['scenario']
if scenario=='fingerprint':
    print(hashlib.sha256(json.dumps(m.snapshot(dsn),sort_keys=True).encode()).hexdigest())
    raise SystemExit(0)
if scenario=='slow_schema':
    original=m.source_tables
    def delayed(conn,preflight=False):
        if preflight:
            with m.preflight_operation('RA_SCHEMA'):conn.execute('SELECT pg_sleep(10)')
        return original(conn,preflight=preflight)
    m.source_tables=delayed
if scenario in ('slow_table','slow_sequence','slow_list'):
    original=m.preflight_read_access
    class Delayed:
        def __init__(self,conn):self.conn=conn
        def execute(self,query,*args,**kwargs):
            text=query.as_string(self.conn) if isinstance(query,m.sql.Composable) else query
            delay=(scenario=='slow_table' and text.startswith('SELECT * FROM public.')) or (scenario=='slow_sequence' and text.startswith('SELECT last_value,is_called')) or (scenario=='slow_list' and text.startswith('SELECT tablename FROM pg_tables'))
            return self.conn.execute('SELECT pg_sleep(10)') if delay else self.conn.execute(query,*args,**kwargs)
    m.preflight_read_access=lambda conn:original(Delayed(conn))
started=time.monotonic()
try:
    result=m.supervise_check_source(dsn,progress=lambda stage:print('GRIT_PREFLIGHT_STAGE:'+stage,flush=True))
except m.DiagnosticFailure as error:
    expected={'locked_table':('RA_TABLE_LOCK','LOCK_UNAVAILABLE'),'slow_schema':('RA_SCHEMA','TIMEOUT'),'slow_table':('RA_TABLE_READ','TIMEOUT'),'slow_list':('RA_TABLE_LIST','TIMEOUT'),'slow_sequence':('RA_SEQUENCE_READ','TIMEOUT')}
    assert (error.stage,error.category)==expected.get(scenario), 'unexpected fixed diagnostic '+str(error)
    assert time.monotonic()-started<8
    print('PASS '+scenario+': '+error.stage+':'+error.category)
else:
    assert scenario=='success'
    assert result['server_version']//10000==16
    print('PASS success: all READ_ACCESS substages completed; no dump created')
'''


def validate_fixture(marker):
    if len(marker)!=32 or any(c not in '0123456789abcdef' for c in marker):raise RuntimeError('Invalid synthetic fixture marker')
    network='grit-deadline-'+marker;source=network+'-pg16';tool='grit-source-check-'+marker
    if docker('inspect','--format','{{ index .Config.Labels "grit.migration.preflight" }}',tool,capture_output=True,text=True).stdout.strip()!=marker:raise RuntimeError('Fixture label mismatch')
    if docker('network','inspect','--format','{{.Internal}}',network,capture_output=True,text=True).stdout.strip()!='true':raise RuntimeError('Fixture network must be internal')
    for name in (source,tool):
        networks=json.loads(docker('inspect','--format','{{json .NetworkSettings.Networks}}',name,capture_output=True,text=True).stdout)
        if set(networks)!={network}:raise RuntimeError('Unexpected fixture network attachment')
    return network,source,tool


def create_fixture():
    marker=uuid.uuid4().hex;network='grit-deadline-'+marker;source=network+'-pg16';tool='grit-source-check-'+marker
    folder=Path(tempfile.mkdtemp(prefix='grit-read-access-',dir='/tmp'))
    for name in ('tls','source','bundles'):(folder/name).mkdir(mode=0o700)
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1','-subj','/CN=render-test','-addext','subjectAltName=DNS:render-test','-keyout',str(folder/'tls/server.key'),'-out',str(folder/'tls/server.crt')],check=True,capture_output=True)
    docker('network','create','--internal',network,capture_output=True)
    docker('run','-d','--name',source,'--network',network,'--network-alias','render-test','-v',str(folder/'source')+':/var/lib/postgresql/data','-v',str(folder/'tls')+':/tls:ro','-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit','-e','POSTGRES_PASSWORD=synthetic-only','--entrypoint','sh','postgres:16-bookworm','-c','cp /tls/server.key /tmp/server.key && cp /tls/server.crt /tmp/server.crt && chown postgres:postgres /tmp/server.key && chmod 600 /tmp/server.key && chmod 644 /tmp/server.crt && exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/server.crt -c ssl_key_file=/tmp/server.key',capture_output=True)
    wait(source)
    schema="CREATE TABLE employees(id text PRIMARY KEY,name text); CREATE TABLE shifts(id text PRIMARY KEY,employee_id text,ended_at text); CREATE TABLE activities(id text PRIMARY KEY,shift_id text,employee_id text,ended_at text); CREATE TABLE scans(id text PRIMARY KEY,shift_id text,employee_id text,barcode text,product_barcode text); CREATE TABLE operation_events(id text PRIMARY KEY); CREATE SEQUENCE sequence_probe; INSERT INTO employees VALUES ('e1','Synthetic');"
    docker('exec',source,'psql','-U','grit','-d','grit','-v','ON_ERROR_STOP=1','-c',schema,capture_output=True)
    docker('run','-d','--name',tool,'--network',network,'--label','grit.migration.preflight='+marker,'--user',str(os.getuid())+':'+str(os.getgid()),'--read-only','--tmpfs','/tmp','--cap-drop=ALL','-v',str(folder/'bundles')+':/bundles','-v',str(folder/'tls/server.crt')+':/etc/ssl/certs/ca-certificates.crt:ro','--entrypoint','python3','grit-migration:ops','-c','import time;time.sleep(600)',capture_output=True)
    print('Created synthetic fixture',marker,folder)
    return marker


def wait(source):
    for _ in range(30):
        result=subprocess.run(DOCKER+['exec',source,'pg_isready','-U','grit','-d','grit'],env=ENV,capture_output=True,timeout=5)
        if result.returncode==0:return
        time.sleep(1)
    raise RuntimeError('Synthetic PostgreSQL startup failed')


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--reuse-fixture');args=parser.parse_args()
    marker=args.reuse_fixture or create_fixture();network,source,tool=validate_fixture(marker)
    try:
        docker('start',source,tool,capture_output=True);wait(source)
        def probe(scenario):
            result=docker('exec','-i',tool,'python3','-c',BOOTSTRAP,input=json.dumps({'module':(ROOT/'deploy/migration/migration_tool.py').read_text(),'url':'postgresql://grit:synthetic-only@render-test/grit','scenario':scenario}),text=True,capture_output=True)
            if scenario!='fingerprint':print(result.stdout.strip())
            assert 'synthetic-only' not in result.stdout+result.stderr
            return result.stdout.strip()
        before=probe('fingerprint');probe('success')
        blocker=subprocess.Popen(DOCKER+['exec','-i',source,'psql','-X','-q','-At','-U','grit','-d','grit','-v','ON_ERROR_STOP=1'],env=ENV,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            blocker.stdin.write("SET lock_timeout='1s'; SET statement_timeout='15s'; BEGIN; LOCK TABLE employees IN ACCESS EXCLUSIVE MODE;\n\\echo GRIT_LOCK_READY\n");blocker.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(blocker.stdout,selectors.EVENT_READ)
                if not selector.select(5) or blocker.stdout.readline().strip()!='GRIT_LOCK_READY':raise RuntimeError('Synthetic blocker was not ready')
            probe('locked_table')
        finally:
            if blocker.poll() is None:
                blocker.stdin.write('ROLLBACK;\n\\q\n');blocker.stdin.flush()
            blocker.communicate(timeout=5)
        for scenario in ('slow_schema','slow_list','slow_table','slow_sequence'):probe(scenario)
        assert probe('fingerprint')==before
        print('PASS: real lock + four slow SQL cases; source content/schema/sequences unchanged')
    finally:
        for name in (tool,source):docker('stop','--time','2',name,capture_output=True)
        print('Synthetic containers/network/database files/archives retained:',network)


if __name__=='__main__':main()
