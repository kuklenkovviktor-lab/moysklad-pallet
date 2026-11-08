"""Explicitly authorized, read-only PostgreSQL export and NEW-database trial restore.

Never prints DSNs, never drops databases, never starts GRIT or changes traffic.
"""
import argparse
from contextlib import contextmanager
import errno
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


class SchemaRefused(Refused):
    pass


STAGES={'INPUT','TOOLS','CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT','PG_DUMP','MANIFEST','ARCHIVE_WRITE','ARCHIVE_CHECK','FINALIZE','RESTORE','SOURCE_CHECK','EXPORT'}
CATEGORIES={'TLS','AUTHENTICATION','PERMISSION','SCHEMA','VERSION','DISK_FULL','IO','TOOL_MISSING','PROCESS_EXIT','TIMEOUT','EMPTY_ARCHIVE','INVALID_ARCHIVE','REFUSED','UNKNOWN','DESTINATION_EXISTS','CONNECTION','READ_ONLY_REFUSAL'}


class DiagnosticFailure(Refused):
    def __init__(self,stage,category):
        self.stage=stage if stage in STAGES else 'INPUT'
        self.category=category if category in CATEGORIES else 'UNKNOWN'
        super().__init__('GRIT_EXPORT_FAILED:'+self.stage+':'+self.category)


def failure_category(error):
    """Only structured codes or recognized primary client diagnostics; never echo."""
    state=getattr(error,'sqlstate',None)
    codes={'28P01':'AUTHENTICATION','28000':'AUTHENTICATION','42501':'PERMISSION',
           '42P01':'SCHEMA','42703':'SCHEMA','3F000':'SCHEMA','53100':'DISK_FULL',
           '58030':'IO','25006':'READ_ONLY_REFUSAL'}
    if state in codes:return codes[state]
    if isinstance(error,OSError):
        if error.errno in (errno.ENOSPC,errno.EDQUOT):return 'DISK_FULL'
        if error.errno in (errno.EACCES,errno.EPERM):return 'PERMISSION'
        if isinstance(error,FileExistsError):return 'DESTINATION_EXISTS'
        if isinstance(error,FileNotFoundError):return 'IO'
        return 'IO'
    if isinstance(error,subprocess.TimeoutExpired):return 'TIMEOUT'
    if isinstance(error,SchemaRefused):return 'SCHEMA'
    if isinstance(error,Refused):return 'REFUSED'
    if not isinstance(error,(psycopg.OperationalError,subprocess.CalledProcessError)):
        return 'CONNECTION' if state and state.startswith('08') else 'UNKNOWN'
    detail=error.stderr if isinstance(error,subprocess.CalledProcessError) else str(error)
    if isinstance(detail,bytes):detail=detail.decode('utf-8',errors='replace')
    detail=detail or ''
    # libpq has no SQLSTATE for certificate verification failures. These are
    # explicit verifier diagnostics, not a guess based on the word "ssl".
    if re.search(r'^pg_dump: error: query failed: ERROR:\s+permission denied for (?:table|sequence|schema|database|relation)\b',detail,re.M):return 'PERMISSION'
    if re.search(r'(?:^|failed: )FATAL:\s+password authentication failed for user',detail,re.M):return 'AUTHENTICATION'
    if re.search(r'(?:^|failed: )FATAL:\s+no pg_hba.conf entry',detail,re.M):return 'PERMISSION'
    if re.search(r'(?:^|failed: )(?:SSL error: certificate verify failed|server certificate .* does not match host name|root certificate file .* does not exist|could not (?:read|open) root certificate file)',detail,re.M):return 'TLS'
    # psql VERBOSITY=sqlstate, or pg_dump's primary server error line.
    match=re.search(r'^.*(?:ERROR|FATAL):\s+([0-9A-Z]{5})(?:\s|$)',detail,re.M)
    if match and match[1] in codes:return codes[match[1]]
    if re.search(r'^pg_dump: error: (?:aborting because of server version mismatch|server version:.*pg_dump version:)',detail,re.M):return 'VERSION'
    if re.search(r'^pg_dump: error: (?:could not write to output file|could not close output file|could not write to file "[^"\r\n]*"): (?:No space left on device|Disk quota exceeded)\s*$',detail,re.M):return 'DISK_FULL'
    if re.search(r'^pg_restore: error: (?:input file (?:does not appear to be a valid archive|is too short)|did not find magic string in file header)',detail,re.M):return 'INVALID_ARCHIVE'
    if isinstance(error,subprocess.CalledProcessError):return 'PROCESS_EXIT'
    if state and state.startswith('08'):return 'CONNECTION'
    return 'UNKNOWN'


@contextmanager
def at_stage(stage):
    try:yield
    except DiagnosticFailure:raise
    except Exception as error:
        raise DiagnosticFailure(stage,failure_category(error)) from None


@contextmanager
def without_pg_environment():
    # This single-threaded CLI must resolve the same DSN for psycopg and tools.
    saved={key:value for key,value in os.environ.items() if key.startswith('PG')}
    for key in saved:os.environ.pop(key)
    try:yield
    finally:
        for key in list(os.environ):
            if key.startswith('PG'):os.environ.pop(key)
        os.environ.update(saved)


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
    with without_pg_environment():
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


def source_tables(conn):
    # Do not silently claim a complete migration for unverified extra schemas.
    extra=conn.execute("SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','S','m','f') AND n.nspname NOT IN ('public','information_schema') AND n.nspname NOT LIKE 'pg_%' LIMIT 1").fetchone()
    if extra: raise SchemaRefused('Non-public storage requires a separate migration audit')
    if conn.execute("SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('m','f') LIMIT 1").fetchone():
        raise SchemaRefused('Materialized views or foreign tables require a separate migration audit')
    if conn.execute('SELECT 1 FROM pg_largeobject_metadata LIMIT 1').fetchone():
        raise SchemaRefused('Large objects require separate content verification')
    if conn.execute("SELECT 1 FROM pg_policies WHERE schemaname='public' LIMIT 1").fetchone():
        raise SchemaRefused('Row security policies require a separate roles/ACL audit')
    if conn.execute("SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prosecdef LIMIT 1").fetchone():
        raise SchemaRefused('SECURITY DEFINER functions require a separate owner/privilege audit')
    tables=[r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")]
    if not REQUIRED.issubset(tables):
        raise SchemaRefused('Required GRIT tables are missing')
    return tables


def manifest(conn):
    tables=source_tables(conn)
    result={'format':1,'server_version':conn.info.server_version,'tables':{},'integrity':integrity(conn,tables)}
    result['database']=conn.execute("SELECT pg_encoding_to_char(encoding),datcollate,datctype,datlocprovider FROM pg_database WHERE datname=current_database()").fetchone()
    for table in tables:
        columns=conn.execute("SELECT column_name,data_type,udt_name,is_nullable,column_default FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",(table,)).fetchall()
        constraints=conn.execute("SELECT c.conname,pg_get_constraintdef(c.oid),c.convalidated FROM pg_constraint c JOIN pg_class t ON t.oid=c.conrelid JOIN pg_namespace n ON n.oid=t.relnamespace WHERE n.nspname='public' AND t.relname=%s ORDER BY c.conname",(table,)).fetchall()
        indexes=conn.execute("SELECT indexname,indexdef FROM pg_indexes WHERE schemaname='public' AND tablename=%s ORDER BY indexname",(table,)).fetchall()
        details=conn.execute("SELECT a.attname,format_type(a.atttypid,a.atttypmod),a.attnotnull,a.attidentity,a.attgenerated,pg_get_expr(d.adbin,d.adrelid),cn.nspname,cl.collname FROM pg_attribute a JOIN pg_class t ON t.oid=a.attrelid JOIN pg_namespace n ON n.oid=t.relnamespace LEFT JOIN pg_attrdef d ON d.adrelid=t.oid AND d.adnum=a.attnum LEFT JOIN pg_collation cl ON cl.oid=a.attcollation LEFT JOIN pg_namespace cn ON cn.oid=cl.collnamespace WHERE n.nspname='public' AND t.relname=%s AND a.attnum>0 AND NOT a.attisdropped ORDER BY a.attnum",(table,)).fetchall()
        value=hashlib.sha256();count=0
        # Stable complete-row comparison, including nullable values and duplicates.
        # The sort may spill to disk on large tables: this is an explicit audit tool.
        with conn.cursor(name='fingerprint') as cursor:
            cursor.execute(sql.SQL('SELECT to_jsonb(t)::text FROM public.{} t ORDER BY to_jsonb(t)::text COLLATE "C"').format(sql.Identifier(table)))
            for row in cursor:
                value.update(row[0].encode('utf-8')+b'\n');count+=1
        result['tables'][table]={'rows':count,'sha256':value.hexdigest(),'columns':columns,'column_details':details,'constraints':constraints,'indexes':indexes}
    result['extensions']=conn.execute('SELECT extname,extversion FROM pg_extension ORDER BY extname').fetchall()
    result['sequences']=sequence_state(conn)
    result['schema_objects']={
        'views':conn.execute("SELECT viewname,definition FROM pg_views WHERE schemaname='public' ORDER BY viewname").fetchall(),
        'triggers':conn.execute("SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid),t.tgenabled FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname").fetchall(),
        'identities':conn.execute("SELECT table_name,column_name,is_identity,identity_generation FROM information_schema.columns WHERE table_schema='public' AND is_identity='YES' ORDER BY table_name,column_name").fetchall(),
        'functions':conn.execute("SELECT p.proname,pg_get_function_identity_arguments(p.oid),pg_get_functiondef(p.oid) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND p.prokind IN ('f','p') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e') ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)").fetchall(),
        'enums':conn.execute("SELECT t.typname,e.enumlabel,e.enumsortorder::text FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace JOIN pg_enum e ON e.enumtypid=t.oid WHERE n.nspname='public' ORDER BY t.typname,e.enumsortorder").fetchall(),
    }
    return json.loads(json.dumps(result))  # Normalize tuples before comparison.


def sequence_state(conn):
    result={}
    for name,start,minimum,maximum,increment,cycle,cache in conn.execute("SELECT sequencename,start_value,min_value,max_value,increment_by,cycle,cache_size FROM pg_sequences WHERE schemaname='public' ORDER BY sequencename"):
        value,called=conn.execute(sql.SQL('SELECT last_value,is_called FROM public.{}').format(sql.Identifier(name))).fetchone()
        result[name]=[start,minimum,maximum,increment,cycle,cache,value,called]
    return result


def snapshot(dsn):
    with connection(dsn) as conn:
        begin_readonly(conn)
        return manifest(conn)


def client_versions():
    versions={}
    for name in ('pg_dump','pg_restore','psql'):
        with at_stage('TOOLS'):
            try:
                version=subprocess.run([name,'--version'],env=pg_environment(None),check=True,capture_output=True,text=True,timeout=10).stdout
            except FileNotFoundError:
                raise DiagnosticFailure('TOOLS','TOOL_MISSING') from None
            match=re.fullmatch(re.escape(name)+r' \(PostgreSQL\) (\d+)(?:\.[^\r\n ]+)?(?: [^\r\n]*)?\n?',version)
            if not match:raise DiagnosticFailure('TOOLS','VERSION')
            versions[name]=int(match[1])
    if len(set(versions.values()))!=1:raise DiagnosticFailure('TOOLS','VERSION')
    return versions


def client_major():
    return client_versions()['pg_dump']


def pg_environment(dsn):
    environment={key:value for key,value in os.environ.items() if not key.startswith('PG')}
    environment['LC_ALL']='C'
    names={'dbname':'PGDATABASE','host':'PGHOST','hostaddr':'PGHOSTADDR','port':'PGPORT','user':'PGUSER','password':'PGPASSWORD','sslmode':'PGSSLMODE','sslrootcert':'PGSSLROOTCERT','sslcert':'PGSSLCERT','sslkey':'PGSSLKEY','options':'PGOPTIONS','connect_timeout':'PGCONNECT_TIMEOUT','service':'PGSERVICE','application_name':'PGAPPNAME','target_session_attrs':'PGTARGETSESSIONATTRS'}
    for key,value in (conninfo_to_dict(dsn).items() if dsn else ()):
        if key not in names:
            raise Refused('Unsupported libpq option; review secure client configuration')
        environment[names[key]]=str(value)
    return environment


def pg_command(command, dsn, stage='RESTORE', timeout=3600):
    with at_stage(stage):
        try:
            return subprocess.run(command,env=pg_environment(dsn),check=True,capture_output=True,timeout=timeout).stdout
        except FileNotFoundError:
            raise DiagnosticFailure(stage,'TOOL_MISSING') from None


def source_snapshot(conn,major):
    with at_stage('SNAPSHOT'):
        begin_readonly(conn)
        if conn.info.server_version//10000>major:
            raise DiagnosticFailure('TOOLS','VERSION')
        return conn.execute('SELECT pg_export_snapshot()').fetchone()[0]


def source_connection(dsn):
    with at_stage('CONNECT'):
        conn=connection(dsn)
        return conn


def check_source(dsn):
    versions=client_versions()
    with source_connection(dsn) as conn:
        snapshot_id=source_snapshot(conn,versions['pg_dump'])
        with at_stage('READ_ACCESS'):
            conn.execute("SET LOCAL lock_timeout='5s'")
            conn.execute("SET LOCAL statement_timeout='60s'")
            tables=source_tables(conn)
            for table in tables:
                conn.execute(sql.SQL('LOCK TABLE public.{} IN ACCESS SHARE MODE').format(sql.Identifier(table)))
                conn.execute(sql.SQL('SELECT * FROM public.{} LIMIT 0').format(sql.Identifier(table)))
            sequence_state(conn)
        # Native psql links the same system libpq as pg_dump, unlike psycopg's
        # bundled libpq. Import the SAME snapshot; never generate even a schema dump.
        if not re.fullmatch(r'[0-9A-Fa-f]+-[0-9A-Fa-f]+-[0-9]+',snapshot_id):
            raise DiagnosticFailure('SNAPSHOT','UNKNOWN')
        query="BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY; SET TRANSACTION SNAPSHOT '"+snapshot_id+"'; SELECT current_setting('server_version_num'); ROLLBACK;"
        pg_command(['psql','-X','--no-password','--set=ON_ERROR_STOP=1','--set=VERBOSITY=sqlstate','--tuples-only','--no-align','--command','BEGIN READ ONLY; SELECT 1; ROLLBACK;'],dsn,stage='NATIVE_CONNECT',timeout=30)
        result=pg_command(['psql','-X','--no-password','--set=ON_ERROR_STOP=1','--set=VERBOSITY=sqlstate','--tuples-only','--no-align','--command',query],dsn,stage='SNAPSHOT_IMPORT',timeout=30)
        if str(conn.info.server_version).encode() not in result.splitlines():
            raise DiagnosticFailure('SNAPSHOT_IMPORT','UNKNOWN')
        return {'server_version':conn.info.server_version,'client_major':versions['pg_dump'],'tables_checked':len(tables)}


def export(dsn, destination):
    folder=Path(destination)
    with at_stage('ARCHIVE_WRITE'):
        folder.mkdir(mode=0o700)  # Exclusive; failures and partials are retained.
    os.umask(0o077)
    major=client_major()
    with source_connection(dsn) as conn:
        snapshot_id=source_snapshot(conn,major)
        with at_stage('READ_ACCESS'):sequences_before=sequence_state(conn)
        temporary=folder/'database.dump.partial'
        pg_command(['pg_dump','--no-password','--format=custom','--no-owner','--no-acl','--snapshot',snapshot_id,'--file',str(temporary)],dsn,stage='PG_DUMP')
        # A successful exit with an empty/invalid output is NOT an export.
        with at_stage('ARCHIVE_WRITE'):
            size=temporary.stat().st_size
            with temporary.open('rb') as stream:magic=stream.read(5);os.fsync(stream.fileno())
        if size==0:raise DiagnosticFailure('ARCHIVE_CHECK','EMPTY_ARCHIVE')
        if size<512 or magic!=b'PGDMP':raise DiagnosticFailure('ARCHIVE_CHECK','INVALID_ARCHIVE')
        with at_stage('MANIFEST'):data=manifest(conn)
        listing=pg_command(['pg_restore','--list',str(temporary)],dsn,stage='ARCHIVE_CHECK').decode('utf-8')
        if not all(re.search(r' TABLE DATA public '+re.escape(table)+r' ',listing) for table in REQUIRED):
            raise DiagnosticFailure('ARCHIVE_CHECK','INVALID_ARCHIVE')
        with at_stage('MANIFEST'):
            if sequences_before!=data['sequences'] or sequence_state(conn)!=sequences_before:
                raise DiagnosticFailure('MANIFEST','REFUSED')
        with at_stage('FINALIZE'):
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
    for field in ('tables','integrity','extensions','sequences','schema_objects','database'):
        if actual.get(field)!=expected.get(field): problems.append(field+' mismatch')
    if any(actual['integrity'].values()): problems.append('Integrity findings require review before cutover')
    return problems


def approved_empty_request_table(table):
    names=('request_id','scan_id','shift_id','employee_id','barcode','product_barcode')
    return (table.get('rows')==0 and table.get('sha256')==hashlib.sha256(b'').hexdigest()
            and table.get('columns')==[[name,'text','text','NO',None] for name in names]
            and table.get('column_details')==[[name,'text',True,'','',None,'pg_catalog','default'] for name in names]
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
    for field in ('sequences','schema_objects','database'):
        if actual.get(field)!=expected.get(field): problems.append(field+' changed')
    return problems


def compare_restored_startup(expected,actual):
    if actual['server_version']//10000<expected['server_version']//10000:
        return ['Target PostgreSQL is older than source']
    # Only the approved startup schema delta is allowed after a major upgrade.
    baseline=dict(expected,server_version=actual['server_version'])
    return compare_rollout(baseline,actual)


def trial_restore(admin_dsn, bundle, database):
    if not re.fullmatch(r'restore_check_[a-z0-9_]{1,45}',database):
        raise Refused('Target must be a NEW restore_check_* database')
    expected=verify_bundle(bundle)
    with connection(admin_dsn,autocommit=True) as admin:
        if admin.info.server_version//10000<expected['server_version']//10000:
            raise Refused('Target PostgreSQL is older than source')
        # No DROP, --clean or connection to an existing target. Concurrent CREATE
        # also fails exclusively, preserving any previously existing database.
        properties=expected.get('database')
        if not properties or properties[3]!='c':
            raise Refused('Re-export with current tools; non-libc locale providers require separate compatibility review')
        encoding,collate,ctype,_=properties
        admin.execute(sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING {} LC_COLLATE {} LC_CTYPE {} LOCALE_PROVIDER libc').format(
            sql.Identifier(database),sql.Literal(encoding),sql.Literal(collate),sql.Literal(ctype)))
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
    parser.add_argument('action',choices=('snapshot','export','verify','trial-restore','compare-live','compare-restored'))
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
            if args.action in ('compare-live','compare-restored') and args.database:
                if not re.fullmatch(r'restore_check_[a-z0-9_]{1,45}',args.database):
                    raise Refused('Comparison target must be restore_check_*')
                values=conninfo_to_dict(dsn);values['dbname']=args.database;dsn=make_conninfo(**values)
            if args.action=='export': export(dsn,args.bundle)
            elif args.action=='snapshot': write_json(args.bundle,snapshot(dsn))
            elif args.action=='trial-restore': trial_restore(dsn,args.bundle,args.database or '')
            elif args.action in ('compare-live','compare-restored'):
                comparator=compare if args.action=='compare-restored' else (compare_restored_startup if args.database else compare_rollout)
                problems=comparator(verify_bundle(args.bundle),snapshot(dsn))
                if problems: raise Refused('; '.join(problems))
        print('Migration check completed; no GRIT process or traffic was started')
    except Exception as error:
        # psycopg exceptions may include secrets, hostnames or record values.
        message=str(error) if isinstance(error,Refused) else 'Migration check failed; inspect access/schema in a secure local session'
        raise SystemExit(message) from None


if __name__=='__main__': main()
