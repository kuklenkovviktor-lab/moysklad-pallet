"""Explicitly authorized, read-only PostgreSQL export and NEW-database trial restore.

Never prints DSNs, never drops databases, never starts GRIT or changes traffic.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

REQUIRED = {'employees','shifts','activities','scans','operation_events'}


class Refused(Exception):
    pass


def checksum(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value,stream,sort_keys=True,indent=2,ensure_ascii=False)
        stream.flush();os.fsync(stream.fileno())


def connection(dsn, **kwargs):
    values=conninfo_to_dict(dsn)
    host=values.get('host','')
    local=not host or host.startswith('/') or host in ('db','localhost','127.0.0.1','::1')
    if not local and values.get('sslmode')!='verify-full':
        raise Refused('Remote PostgreSQL requires sslmode=verify-full and a trusted CA')
    return psycopg.connect(dsn, connect_timeout=10, **kwargs)


def begin_readonly(conn):
    conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
    conn.execute("SET LOCAL TIME ZONE 'UTC'")
    conn.execute('SET LOCAL search_path=pg_catalog,public')
    conn.execute("SET LOCAL DateStyle='ISO, YMD'")
    conn.execute('SET LOCAL extra_float_digits=3')


def integrity(conn, tables):
    probes={
      'duplicate_wb_barcodes': 'SELECT count(*) FROM (SELECT barcode FROM scans GROUP BY barcode HAVING count(*)>1) x',
      'orphan_scans': 'SELECT count(*) FROM scans s LEFT JOIN shifts h ON h.id=s.shift_id LEFT JOIN employees e ON e.id=s.employee_id WHERE h.id IS NULL OR e.id IS NULL OR h.employee_id<>s.employee_id',
      'orphan_activities': 'SELECT count(*) FROM activities a LEFT JOIN shifts h ON h.id=a.shift_id WHERE h.id IS NULL OR h.employee_id<>a.employee_id',
      'duplicate_open_shifts': 'SELECT count(*) FROM (SELECT employee_id FROM shifts WHERE ended_at IS NULL GROUP BY employee_id HAVING count(*)>1) x',
      'duplicate_open_activities': 'SELECT count(*) FROM (SELECT shift_id FROM activities WHERE ended_at IS NULL GROUP BY shift_id HAVING count(*)>1) x'}
    if 'receiving_confirmations' in tables:
        probes['unresolved_receiving']="SELECT count(*) FROM receiving_confirmations WHERE status IN ('processing','uncertain')"
    if 'fbs_scan_requests' in tables:
        probes['orphan_fbs_requests']='SELECT count(*) FROM fbs_scan_requests r LEFT JOIN scans s ON s.id=r.scan_id WHERE s.id IS NULL OR s.employee_id<>r.employee_id OR s.shift_id<>r.shift_id OR s.barcode<>r.barcode OR s.product_barcode<>r.product_barcode'
    return {name:conn.execute(query).fetchone()[0] for name,query in probes.items()}


def manifest(conn):
    tables=[r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    if not REQUIRED.issubset(tables):
        raise Refused('Required GRIT tables are missing')
    result={'format':1,'server_version':conn.info.server_version,'tables':{},'integrity':integrity(conn,tables)}
    for table in tables:
        columns=conn.execute("SELECT column_name,data_type,udt_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,)).fetchall()
        constraints=conn.execute("SELECT c.conname,pg_get_constraintdef(c.oid),c.convalidated FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND t.relname=%s ORDER BY c.conname",(table,)).fetchall()
        indexes=conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%s ORDER BY indexname",(table,)).fetchall()
        value=hashlib.sha256();count=0
        # Stable complete-row comparison, including nullable values and duplicates.
        # The sort may spill to disk on large tables: this is an explicit audit tool.
        with conn.cursor(name='fingerprint') as cursor:
            cursor.execute(sql.SQL('SELECT to_jsonb(t)::text FROM public.{} t ORDER BY to_jsonb(t)::text COLLATE "C"').format(sql.Identifier(table)))
            for row in cursor:
                value.update(row[0].encode('utf-8')+b'\n');count+=1
        result['tables'][table]={'rows':count,'sha256':value.hexdigest(),'columns':columns,'constraints':constraints,'indexes':indexes}
    result['extensions']=conn.execute('SELECT extname,extversion FROM pg_extension ORDER BY extname').fetchall()
    return json.loads(json.dumps(result))  # Normalize tuples before comparison.


def snapshot(dsn):
    with connection(dsn) as conn:
        begin_readonly(conn)
        return manifest(conn)


def client_major():
    version=subprocess.run(['pg_dump','--version'],check=True,capture_output=True,text=True).stdout
    match=re.search(r'(\d+)\.',version)
    if not match: raise Refused('Cannot determine pg_dump version')
    return int(match[1])


def pg_command(command, dsn):
    environment=dict(os.environ)
    names={'dbname':'PGDATABASE','host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','options':'PGOPTIONS','connect_timeout':'PGCONNECT_TIMEOUT','service':'PGSERVICE','application_name':'PGAPPNAME','target_session_attrs':'PGTARGETSESSIONATTRS'}
    for key,value in conninfo_to_dict(dsn).items():
        if key not in names:
            raise Refused('Unsupported libpq option; review secure client configuration')
        environment[names[key]]=str(value)
    try:
        subprocess.run(command, env=environment,check=True,capture_output=True,timeout=3600)
    except (subprocess.SubprocessError, OSError):
        raise Refused('PostgreSQL command failed; existing databases and archives were not removed') from None


def export(dsn, destination):
    folder=Path(destination)
    folder.mkdir(mode=0o700)  # Exclusive: never overwrite a previous bundle.
    os.umask(0o077)
    with connection(dsn) as conn:
        begin_readonly(conn)
        if conn.info.server_version//10000>client_major():
            raise Refused('pg_dump major version is older than source PostgreSQL')
        snapshot_id=conn.execute('SELECT pg_export_snapshot()').fetchone()[0]
        temporary=folder/'database.dump.partial'
        pg_command(['pg_dump','--format=custom','--no-owner','--no-acl','--snapshot',snapshot_id,'--file',str(temporary)],dsn)
        # Manifest and dump share EXACTLY the same exported database snapshot.
        data=manifest(conn)
        pg_command(['pg_restore','--list',str(temporary)],dsn)
        write_json(folder/'manifest.json',data)
        temporary.rename(folder/'database.dump')
        write_json(folder/'checksums.json', {name:checksum(folder/name) for name in ('database.dump','manifest.json')})
    return data


def verify_bundle(folder):
    folder=Path(folder)
    expected=json.loads((folder/'checksums.json').read_text())
    if set(expected)!={'database.dump','manifest.json'}:
        raise Refused('Invalid bundle checksum manifest')
    for name,value in expected.items():
        if (folder/name).is_symlink() or checksum(folder/name)!=value:
            raise Refused('Bundle SHA-256 mismatch')
    data=json.loads((folder/'manifest.json').read_text())
    if data.get('format')!=1: raise Refused('Unsupported manifest format')
    return data


def compare(expected, actual):
    problems=[]
    if actual['server_version']//10000<expected['server_version']//10000:
        problems.append('Target PostgreSQL is older than source')
    for field in ('tables','integrity','extensions'):
        if actual[field]!=expected[field]: problems.append(field+' mismatch')
    if any(actual['integrity'].values()): problems.append('Integrity findings require review before cutover')
    return problems


def approved_empty_request_table(table):
    names=('request_id','scan_id','shift_id','employee_id','barcode','product_barcode')
    return (table.get('rows')==0 and table.get('sha256')==hashlib.sha256(b'').hexdigest()
            and table.get('columns')==[[name,'text','text','NO',None] for name in names]
            and table.get('constraints')==[['fbs_scan_requests_pkey','PRIMARY KEY (request_id)',True]]
            and table.get('indexes')==[['fbs_scan_requests_pkey','CREATE UNIQUE INDEX fbs_scan_requests_pkey ON public.fbs_scan_requests USING btree (request_id)']])


def compare_rollout(expected, actual):
    """No existing data/schema drift; only PR93's exact NEW empty table allowed."""
    problems=[]
    if actual['server_version']!=expected['server_version']:
        problems.append('PostgreSQL version changed during application-only rollout')
    for name,before in expected['tables'].items():
        if actual['tables'].get(name)!=before:
            problems.append('Existing table changed: '+name)
    extra=set(actual['tables'])-set(expected['tables'])
    if extra:
        if extra!={'fbs_scan_requests'} or not approved_empty_request_table(actual['tables']['fbs_scan_requests']):
            problems.append('Unapproved schema addition or nonempty new request ledger')
    previous=expected['integrity'];current=actual['integrity']
    if any(current.values()) or any(previous.values()):
        problems.append('Integrity findings block rollout acceptance')
    if any(current.get(name)!=value for name,value in previous.items()):
        problems.append('Integrity counters changed')
    extra_checks=set(current)-set(previous)
    if extra_checks-({'orphan_fbs_requests'} if extra=={'fbs_scan_requests'} else set()):
        problems.append('Unexpected integrity checks')
    if actual['extensions']!=expected['extensions']:
        problems.append('Extensions changed')
    return problems


def trial_restore(admin_dsn, bundle, database):
    if not re.fullmatch(r'restore_check_[a-z0-9_]{1,45}',database):
        raise Refused('Target must be a NEW restore_check_* database')
    expected=verify_bundle(bundle)
    with connection(admin_dsn,autocommit=True) as admin:
        if admin.info.server_version//10000<expected['server_version']//10000:
            raise Refused('Target PostgreSQL is older than source')
        # No DROP, --clean or connection to an existing target. Concurrent CREATE
        # also fails exclusively, preserving any previously existing database.
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(database)))
    values=conninfo_to_dict(admin_dsn);values['dbname']=database
    target=make_conninfo(**values)
    pg_command(['pg_restore','--exit-on-error','--single-transaction','--no-owner','--no-acl','--dbname',database,str(Path(bundle)/'database.dump')],target)
    actual=snapshot(target)
    problems=compare(expected,actual)
    if problems: raise Refused('; '.join(problems))
    return actual


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('snapshot','export','verify','trial-restore','compare-live'))
    parser.add_argument('--dsn-env',default='GRIT_MIGRATION_DSN')
    parser.add_argument('--confirm-read-only-access',action='store_true')
    parser.add_argument('--bundle',required=True)
    parser.add_argument('--database')
    args=parser.parse_args()
    try:
        if args.action=='verify':
            verify_bundle(args.bundle)
        else:
            if not args.confirm_read_only_access: raise Refused('Explicit connection authorization is required')
            dsn=os.environ.get(args.dsn_env)
            if not dsn: raise Refused('Configured DSN variable is missing')
            if args.action=='export': export(dsn,args.bundle)
            elif args.action=='snapshot': write_json(args.bundle,snapshot(dsn))
            elif args.action=='trial-restore': trial_restore(dsn,args.bundle,args.database or '')
            elif args.action=='compare-live':
                problems=compare_rollout(verify_bundle(args.bundle),snapshot(dsn))
                if problems: raise Refused('; '.join(problems))
        print('Migration check completed; no GRIT process or traffic was started')
    except Exception as error:
        # psycopg exceptions may include secrets, hostnames or record values.
        message=str(error) if isinstance(error,Refused) else 'Migration check failed; inspect access/schema in a secure local session'
        raise SystemExit(message) from None


if __name__=='__main__': main()
