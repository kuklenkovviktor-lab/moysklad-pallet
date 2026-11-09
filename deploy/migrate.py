"""Interactive, local-Docker migration commands. No SSH, DNS or remote deployment.

Run from the reviewed repository: python3 deploy/migrate.py --help
External credentials are read without echo and passed only through stdin.
"""
import argparse
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import signal
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
SELECTORS=('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')
SECRETS=('SECRET_KEY','WMS_BADGE_SECRET','WMS_TERMINAL_SECRET','MANAGER_WEB_PASSWORD')
INTEGRATIONS=('MOYSKLAD_TOKEN','WB_API_TOKEN','ONIX_PRINT_AGENT_TOKEN','GRIT_MP_WB_TOKEN',
              'GRIT_MP_OZON_CLIENT_ID','GRIT_MP_OZON_API_KEY','GRIT_MP_YANDEX_API_KEY','GRIT_MP_YANDEX_BUSINESS_ID')


class Stop(Exception): pass


def private_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise Stop('Private configuration must be a regular file with permissions 0600')


def secret(prompt):
    if not sys.stdin.isatty(): raise Stop('A real interactive terminal is required for secret entry')
    value=getpass.getpass(prompt)
    if os.name=='posix':
        import termios
        # Discard queued extra pasted lines before returning to Bash.
        termios.tcflush(sys.stdin.fileno(),termios.TCIFLUSH)
    if any(ord(c)<32 or ord(c)==127 for c in value): raise Stop('Multiline/control characters are refused')
    return value


def quote_env(value):
    if any(c in value for c in '\r\n\x00'): raise Stop('Multiline values are refused')
    return json.dumps(value,ensure_ascii=False).replace('$','$$')


def database_name(value):
    if not re.fullmatch(r'restore_check_[a-z0-9_]{1,45}',value):
        raise Stop('Choose a NEW name beginning restore_check_, using lowercase letters, digits and underscores')
    return value


def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''): digest.update(chunk)
    return digest.hexdigest()


def command_environment(extra=None):
    environment={k:v for k,v in os.environ.items() if k not in SELECTORS}
    # Compose interpolation must use its private files, never stale shell secrets.
    for key in (*SECRETS,*INTEGRATIONS,'POSTGRES_PASSWORD','GRIT_DATABASE_NAME','GRIT_DATA_ROOT',
                'GRIT_PORT','COMPOSE_PROJECT_NAME','COMPOSE_FILE','GRIT_OPERATIONS_DIR'):
        environment.pop(key,None)
    for key in list(environment):
        if key.startswith(('GRIT_','BACKUP_','FBS_','WB_','SUPERVISOR_','MANAGER_LOGIN_')) or key=='BOOTSTRAP_CACHE_SECONDS':
            environment.pop(key,None)
    environment.update(extra or {})
    return environment


def run(args, **kwargs):
    environment=command_environment(kwargs.pop('extra_env',{}))
    try:
        return subprocess.run(args,cwd=ROOT,env=environment,check=True,**kwargs)
    except subprocess.CalledProcessError as error:
        output=error.stderr or ''
        if isinstance(output,bytes): output=output.decode('utf-8',errors='replace')
        staged=re.search(r'GRIT_EXPORT_FAILED:(INPUT|TOOLS|CONNECT|SNAPSHOT|READ_ACCESS|NATIVE_CONNECT|SNAPSHOT_IMPORT|PG_DUMP|MANIFEST|ARCHIVE_WRITE|ARCHIVE_CHECK|FINALIZE|RESTORE|SOURCE_CHECK|EXPORT|CLOSE):(TLS|AUTHENTICATION|PERMISSION|SCHEMA|VERSION|DISK_FULL|IO|TOOL_MISSING|PROCESS_EXIT|TIMEOUT|EMPTY_ARCHIVE|INVALID_ARCHIVE|REFUSED|UNKNOWN|DESTINATION_EXISTS|CONNECTION|READ_ONLY_REFUSAL|CANCELLED)\b',output)
        if staged:raise Stop('Source/export failed at '+staged[1]+'; category '+staged[2]+'. No raw database error is printed; existing archives are retained.') from None
        match=re.search(r'GRIT_EXPORT_FAILED:(DNS|TLS|AUTHENTICATION|TCP_OR_FIREWALL|DESTINATION_EXISTS|URL_OR_SCHEMA_OR_ARCHIVE|SOURCE_OR_ARCHIVE)\b',output)
        if match:raise Stop('Export diagnosis: '+match[1]+'. See the diagnosis table in the migration guide. No SSH tunnel is used.') from None
        raise Stop('Command failed; stopped without removing databases, volumes or archives. Check Docker/network/permissions privately.') from None
    except (subprocess.SubprocessError,OSError):
        raise Stop('Command failed; stopped without removing databases, volumes or archives. Check Docker/network/permissions privately.') from None


def docker(*args, **kwargs):
    return run(['docker','--host=unix:///var/run/docker.sock',*args],**kwargs)


def compose(*args, live=False, https=False, **kwargs):
    private_file(ROOT/'.env')
    cmd=['docker','--host=unix:///var/run/docker.sock','compose','--project-directory',str(ROOT),'--env-file',str(ROOT/'.env')]
    if live:
        private_file(ROOT/'.env.cutover')
        cmd+=['--env-file',str(ROOT/'.env.cutover')]
    cmd+=['-f',str(ROOT/'compose.yaml'),'-f',str(ROOT/'compose.ops.yaml')]
    if live: cmd+=['-f',str(ROOT/'compose.restore.yaml')]
    if https: cmd+=['-f',str(ROOT/'compose.live.yaml'),'-f',str(ROOT/'compose.https.yaml')]
    extra=kwargs.pop('extra_env',{})
    extra.setdefault('GRIT_OPERATIONS_DIR','/tmp/grit-preflight')
    return run(cmd+list(args),extra_env=extra,**kwargs)


def bundle_path(value):
    path=Path(value).absolute()
    if path.is_symlink() or not path.is_dir(): raise Stop('Choose an existing regular bundle directory')
    return path


def operations(action,bundle,database=None):
    path=bundle_path(bundle)
    args=[action,'--confirm-read-only-access','--bundle','/operations/'+path.name]
    if database: args+=['--database',database_name(database)]
    return compose('run','--rm','--no-deps','--user',str(os.getuid())+':'+str(os.getgid()),'ops',*args,extra_env={'GRIT_OPERATIONS_DIR':str(path.parent)},capture_output=True)


def export_external(destination):
    destination=Path(destination).absolute()
    if destination.resolve().is_relative_to(ROOT.resolve()):raise Stop('Migration data must be outside the Git repository')
    if destination.exists(): raise Stop('Destination exists; choose a new bundle name')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if destination.parent.is_symlink() or destination.parent.stat().st_mode & 0o077:
        raise Stop('Bundle parent directory must be private (0700)')
    source_request('export',destination)
    verify_local(destination)


PREFLIGHT_STAGES={'INPUT','TOOLS','CONNECT','SNAPSHOT','READ_ACCESS','NATIVE_CONNECT','SNAPSHOT_IMPORT','CLOSE','SOURCE_CHECK'}
PREFLIGHT_CATEGORIES={'TLS','AUTHENTICATION','PERMISSION','SCHEMA','VERSION','DISK_FULL','IO','TOOL_MISSING','PROCESS_EXIT','TIMEOUT','REFUSED','UNKNOWN','CONNECTION','READ_ONLY_REFUSAL','CANCELLED'}


def stop_preflight_container(name,marker):
    """Stop only THIS new labeled container. Never remove it or touch a database."""
    if name!='grit-source-check-'+marker or not re.fullmatch(r'[0-9a-f]{32}',marker):return False
    cmd=['docker','--host=unix:///var/run/docker.sock']
    probe=['inspect','--format','{{ index .Config.Labels "grit.migration.preflight" }} {{.State.Running}}',name]
    try:
        status=subprocess.run(cmd+probe,env=command_environment(),check=True,capture_output=True,text=True,timeout=3).stdout.strip()
        if status==marker+' false':return True
        if status!=marker+' true':return False
        subprocess.run(cmd+['stop','--time','2',name],env=command_environment(),check=True,capture_output=True,text=True,timeout=5)
        return subprocess.run(cmd+probe,env=command_environment(),check=True,capture_output=True,text=True,timeout=3).stdout.strip()==marker+' false'
    except (OSError,subprocess.SubprocessError):return False


def run_preflight_container(args,payload,name,marker,total_timeout=150,stopper=None):
    """Stream ONLY allowlisted stage events; bound stdin, Docker wait and cleanup."""
    state={'stage':'CONTAINER_START','error':None};output=[];process=None;failure=None
    deadline=time.monotonic()+total_timeout
    print('check-source: CONTAINER_START',flush=True)
    def stderr_reader(stream):
        try:
            while line:=stream.readline(4096):
                match=re.fullmatch(r'GRIT_PREFLIGHT_STAGE:([A-Z_]+)\n?',line)
                if match and match[1] in PREFLIGHT_STAGES:
                    if state['stage']!=match[1]:print('check-source: '+match[1],flush=True)
                    state['stage']=match[1]
                error=re.fullmatch(r'GRIT_EXPORT_FAILED:([A-Z_]+):([A-Z_]+)\n?',line)
                if error and error[1] in PREFLIGHT_STAGES and error[2] in PREFLIGHT_CATEGORIES:
                    state['error']=(error[1],error[2])
        except (OSError,ValueError):pass
    def stdout_reader(stream):
        try:
            while chunk:=stream.read(1024):
                if sum(map(len,output))<4096:output.append(chunk[:1024])
        except (OSError,ValueError):pass
    def write_input(stream):
        try:stream.write(payload);stream.close()
        except (OSError,ValueError):pass
    threads=[]
    try:
        process=subprocess.Popen(args,cwd=ROOT,env=command_environment(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf-8',errors='replace',start_new_session=True)
        for fn,stream in ((stderr_reader,process.stderr),(stdout_reader,process.stdout),(write_input,process.stdin)):
            thread=threading.Thread(target=fn,args=(stream,),daemon=True);thread.start();threads.append(thread)
        while process.poll() is None:
            if time.monotonic()>=deadline:
                failure=(state['stage'],'TIMEOUT');break
            time.sleep(0.05)
    except KeyboardInterrupt:failure=(state['stage'],'CANCELLED')
    except (OSError,subprocess.SubprocessError):failure=(state['stage'],'UNKNOWN')
    finally:
        if process is not None and process.poll() is None:
            try:os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:pass
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:pass
        stopped=(stopper or stop_preflight_container)(name,marker)
        for thread in threads:thread.join(0.2)
        # Do not close streams while a reader might own their buffered lock.
        if process is not None and all(not thread.is_alive() for thread in threads):
            for stream in (process.stdin,process.stdout,process.stderr):
                if not stream.closed:stream.close()
    failure=failure or state['error']
    if failure is None and (process is None or process.returncode!=0):failure=(state['stage'],'PROCESS_EXIT')
    if not stopped:
        failure=failure or ('CONTAINER_CLEANUP','UNKNOWN')
        suffix=' Container stop unconfirmed; inspect only '+name+'.'
    else:suffix=' Created check-source container is stopped and retained: '+name+'.'
    if failure:raise Stop('check-source failed at '+failure[0]+'; category '+failure[1]+'.'+suffix)
    return subprocess.CompletedProcess(args,process.returncode,stdout=''.join(output),stderr='')


def source_request(action,destination=None):
    value=secret('Render External Database URL (hidden, ONE line, no quotes): ')
    if not value.startswith(('postgresql://','postgres://')) or any(c.isspace() for c in value):
        raise Stop('Paste only the complete external PostgreSQL URL; no ssh, quotes or Bash commands')
    payload={'url':value,'action':action}
    mounts=[]
    if destination is not None:
        payload['destination']='/bundles/'+destination.name
        mounts=['--mount','type=bind,src='+str(destination.parent)+',dst=/bundles']
    args=['run','-i','--user',str(os.getuid())+':'+str(os.getgid()),'--read-only','--tmpfs','/tmp','--cap-drop=ALL',
           *mounts,'--mount','type=bind,src=/etc/ssl/certs,dst=/etc/ssl/certs,readonly',
           '--entrypoint','python3','grit-migration:ops','/tools/input_entrypoint.py']
    if action=='check-source':
        marker=uuid.uuid4().hex;name='grit-source-check-'+marker
        args[1:1]=['--name',name,'--label','grit.migration.preflight='+marker]
        result=run_preflight_container(['docker','--host=unix:///var/run/docker.sock',*args],json.dumps(payload),name,marker)
    else:
        args.insert(1,'--rm')
        result=docker(*args,input=json.dumps(payload),text=True,capture_output=True)
    if action=='check-source':
        # Worker success is numeric/fixed vocabulary only. Do not relay arbitrary output.
        match=re.fullmatch(r'Source check passed: PostgreSQL (\d+); native clients major (\d+); readable public tables (\d+)\. No dump created; full export may still encounter other errors\.\n?',result.stdout)
        if not match:raise Stop('Source check returned an unrecognized result; no raw output is printed')
        print('PASS: source PostgreSQL '+match[1]+', native clients '+match[2]+', readable tables '+match[3]+'. No dump created; export success is not guaranteed.')


def verify_local(path):
    path=bundle_path(path)
    checks=json.loads((path/'checksums.json').read_text())
    if set(checks)!={'database.dump','manifest.json'}: raise Stop('Unexpected checksum manifest')
    for name,expected in checks.items():
        if (path/name).is_symlink() or sha(path/name)!=expected: raise Stop('Checksum mismatch; do not restore')
    docker('run','--rm','--network','none','--read-only','--mount','type=bind,src='+str(path)+',dst=/bundle,readonly',
           '--entrypoint','pg_restore','grit-migration:ops','--list','/bundle/database.dump',capture_output=True)
    print('PASS: archive structure and both SHA-256 checksums')


def pack_files(source,destination):
    source=Path(source)
    if source.is_symlink() or not source.is_dir():raise Stop('Choose an existing regular source directory')
    source=source.resolve();destination=Path(destination).absolute()
    if destination.resolve().is_relative_to(ROOT.resolve()):raise Stop('File bundles must be outside the Git repository')
    if destination.resolve().is_relative_to(source):raise Stop('Archive destination must be outside the source directory')
    files=sorted(source.rglob('*'))
    if any(path.is_symlink() or not (path.is_dir() or path.is_file()) for path in files):
        raise Stop('Symlinks and special files require separate review; no archive created')
    files=[path for path in files if path.is_file()]
    if not files:raise Stop('No regular files found')
    destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    destination.mkdir(mode=0o700)
    entries=[];temporary=destination/'files.tar.gz.partial'
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream,tarfile.open(fileobj=stream,mode='w:gz') as archive:
        for file in files:
            name='files/'+file.relative_to(source).as_posix()
            expected=sha(file);size=file.stat().st_size
            info=archive.gettarinfo(str(file),arcname=name)
            if not info.isfile():raise Stop('Source changed; partial file archive retained')
            with file.open('rb') as content:archive.addfile(info,content)
            if sha(file)!=expected or file.stat().st_size!=size:raise Stop('Source changed; partial file archive retained')
            entries.append({'name':name,'size':size,'sha256':expected})
    temporary.rename(destination/'files.tar.gz')
    (destination/'files-manifest.json').write_text(json.dumps(entries,ensure_ascii=False,indent=2))
    (destination/'files-manifest.json').chmod(0o600)
    (destination/'checksums.json').write_text(json.dumps({name:sha(destination/name) for name in ('files.tar.gz','files-manifest.json')}))
    (destination/'checksums.json').chmod(0o600)
    verify_files(destination)


def verify_files(folder):
    folder=bundle_path(folder)
    checks=json.loads((folder/'checksums.json').read_text())
    if set(checks)!={'files.tar.gz','files-manifest.json'}:raise Stop('Invalid file bundle')
    if any((folder/name).is_symlink() or sha(folder/name)!=value for name,value in checks.items()):raise Stop('File bundle SHA mismatch')
    expected=json.loads((folder/'files-manifest.json').read_text());actual=[]
    with tarfile.open(folder/'files.tar.gz','r:gz') as archive:
        for member in archive:
            if not member.isfile() or not member.name.startswith('files/') or '..' in Path(member.name).parts:
                raise Stop('Unsafe archive member')
            digest=hashlib.sha256()
            with archive.extractfile(member) as content:
                for chunk in iter(lambda:content.read(1024*1024),b''):digest.update(chunk)
            actual.append({'name':member.name,'size':member.size,'sha256':digest.hexdigest()})
    if actual!=expected:raise Stop('File contents differ from inventory')
    print('PASS: file archive and every original file SHA-256; no extraction performed')


def prepare_live(database):
    database_name(database)
    values={'GRIT_DATABASE_NAME':database}
    print('Enter EXISTING Render signing keys. Never generate replacements for migrated badges.')
    for key in SECRETS:
        values[key]=secret(key+' (existing Render value, hidden): ')
        if not values[key]: raise Stop('Signing keys and manager password cannot be empty')
    for key in INTEGRATIONS: values[key]=secret(key+' (hidden; Enter if unused): ')
    for key,default in [('SUPERVISOR_TERMINAL_VERSION','1'),('WB_STRICT_MATCHING','0'),('WB_SYNC_DAYS','5'),('FBS_RATE','2.25'),('FBS_TARGET','1600')]:
        values[key]=input(key+' (match Render; default '+default+'): ').strip() or default
    values['GRIT_ACME_EMAIL']=input('ACME contact email: ').strip()
    values['GRIT_ADMIN_CIDRS']=input('Manager/print-agent public IP CIDRs (space separated): ').strip() or '127.0.0.1/32 ::1/128'
    values['GRIT_MARKETPLACE_LIVE_READ_ENABLED']='0'
    fd=os.open(ROOT/'.env.cutover',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        for key,value in values.items(): stream.write(key+'='+quote_env(value)+'\n')
        stream.flush();os.fsync(stream.fileno())
    print('Created .env.cutover (0600). Existing .env and .env.backup unchanged. Nothing started.')


def preflight():
    private_file(ROOT/'.env')
    for file in ('.env.backup','.env.cutover'):
        if (ROOT/file).exists(): private_file(ROOT/file)
    db=compose('ps','-q','db',capture_output=True,text=True).stdout.strip()
    if not db: raise Stop('Existing Compose db container not found; do not initialize a new one')
    info=json.loads(docker('inspect',db,capture_output=True,text=True).stdout)[0]
    labels=info['Config'].get('Labels',{})
    if labels.get('com.docker.compose.project.working_dir')!=str(ROOT):
        raise Stop('Compose working directory mismatch')
    # Existing installations using extra/unknown overlays require individual review.
    if labels.get('com.docker.compose.project.config_files')!=str(ROOT/'compose.yaml'):
        raise Stop('Existing database uses other Compose files; review configuration before updating')
    mounts=[m for m in info['Mounts'] if m['Destination']=='/var/lib/postgresql/data']
    if len(mounts)!=1 or mounts[0]['Type']!='bind': raise Stop('Expected an existing PostgreSQL bind mount')
    source=Path(mounts[0]['Source'])
    if not source.is_absolute() or source.is_symlink() or not source.exists(): raise Stop('Unsafe data path')
    version=compose('exec','-T','db','psql','-U','grit','-d','grit','-Atc','SHOW server_version_num',capture_output=True,text=True).stdout.strip()
    if not version.isdigit() or int(version)//10000!=17: raise Stop('Existing PostgreSQL must be version 17')
    # Verify evaluated Compose still points at EXACTLY the existing data directory.
    config=json.loads(compose('config','--format','json',extra_env={'GRIT_OPERATIONS_DIR':'/tmp/grit-preflight'},capture_output=True,text=True).stdout)
    expected=[v['source'] for v in config['services']['db']['volumes'] if v['target']=='/var/lib/postgresql/data']
    if expected!=[str(source)] or config['services']['db'].get('ports'): raise Stop('Database storage or exposure changed')
    print('PASS: local PostgreSQL 17, existing bind path and Compose identity; database has no published port')
    return db,source.parent


def update():
    db,data_root=preflight()
    app=compose('ps','-a','-q','app',capture_output=True,text=True).stdout.strip()
    if app:
        config=json.loads(docker('inspect',app,capture_output=True,text=True).stdout)[0]['Config']
        if any(item.startswith('GRIT_DATABASE_NAME=') and item!='GRIT_DATABASE_NAME=grit' for item in config.get('Env',[])):
            raise Stop('This update command is ONLY for the pre-migration test installation; selected restored/live app detected')
    if input('Type UPDATE TEST APP to preserve db and update only app/backup: ')!='UPDATE TEST APP': raise Stop('Cancelled')
    folder=data_root/'migration'/('update-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    folder.mkdir(mode=0o700,parents=True)
    secrets={}
    for name in ('.env','.env.backup','.env.cutover'):
        path=ROOT/name
        if path.exists():
            secrets[name]=sha(path)
            shutil.copyfile(path,folder/name);(folder/name).chmod(0o600)
    # Build before downtime. No db pull/up/recreate is ever issued.
    compose('build','app','backup','ops',extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
    compose('stop','app','backup',extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
    compose('run','--rm','--no-deps','--user',str(os.getuid())+':'+str(os.getgid()),'ops','export','--confirm-read-only-access','--bundle','/operations/baseline',
            extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
    operations('verify',folder/'baseline')
    try:
        compose('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','180','app',
                extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
        operations('compare-live',folder/'baseline')
        if compose('ps','-q','db',capture_output=True,text=True).stdout.strip()!=db: raise Stop('Database container changed')
        if any(sha(ROOT/name)!=value for name,value in secrets.items()): raise Stop('Private configuration changed')
        compose('run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-once.sh',extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
        compose('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','180','backup',extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
    except Exception:
        compose('stop','app','backup',extra_env={'GRIT_OPERATIONS_DIR':str(folder)},capture_output=True)
        raise
    print('PASS: app/backup updated; db container, business records and secret files preserved. Recovery bundle:',folder)


def staged_start(bundle,database,live=False):
    preflight()
    database_name(database)
    private_file(ROOT/'.env.cutover')
    # Inspect without printing the resolved secrets.
    config=json.loads(compose('config','--format','json',live=True,https=live,
        extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True,text=True).stdout)
    if config['services']['app']['environment'].get('GRIT_DATABASE_NAME')!=database: raise Stop('Database differs from .env.cutover')
    if live:
        check_external_receipt(bundle_path(bundle))
        settings=config['services']['backup']['environment']
        if any(str(settings.get(key,'')).lower() not in ('1','true','yes') for key in ('BACKUP_S3_ENABLED','BACKUP_ALERT_REQUIRED')):
            raise Stop('Live start requires S3 backups and required failure notifications enabled')
    operations('compare-live' if live else 'compare-restored',bundle,database)
    if live and input('Type RENDER WRITERS STOPPED to confirm all old app/workers are suspended: ')!='RENDER WRITERS STOPPED':
        raise Stop('Cancelled; no target writers started')
    compose('stop','app','backup',capture_output=True,extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)})
    try:
        if live:
            # A recent test-db archive must NEVER stand in for the first live-db
            # backup. Verify notifications and force a fresh selected-db upload
            # before enabling external integration writes.
            compose('run','--rm','--no-deps','--entrypoint','python3','backup','/scripts/backup_worker.py','notify-test',live=True,
                    extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
            compose('run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-once.sh',live=True,
                    extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
            compose('run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-health.sh',live=True,
                    extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
        compose('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','180','app',live=True,
                https=live,extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
        if not live:
            operations('compare-live',bundle,database)
            print('PASS: restored app healthy, rows preserved, integrations disabled. Loopback only. Stop before warehouse tests.')
        else:
            compose('up','-d','--no-build','--no-deps','--force-recreate','--wait','--wait-timeout','180','backup','caddy',live=True,https=True,
                    extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
            print('Local app/backup started. Verify public TLS and warehouse checklist manually before admitting employees.')
    except Exception:
        compose('stop',*(['app','backup','caddy'] if live else ['app','backup']),live=True,https=live,
                extra_env={'GRIT_OPERATIONS_DIR':str(Path(bundle).absolute().parent)},capture_output=True)
        raise


def check_external_receipt(folder):
    checks=json.loads((folder/'checksums.json').read_text())
    checks['checksums.json']=sha(folder/'checksums.json')
    for receipt in folder.glob('external-copy-*.json'):
        data=json.loads(receipt.read_text())
        objects=data.get('objects',[])
        found={item['key'].rsplit('/',1)[-1]:item['sha256'] for item in objects}
        if len(objects)==3 and found==checks:return
    raise Stop('No complete verified external-copy receipt matching this bundle; do not start live')


def stop_local():
    # Remains usable even if a private env file was damaged or ACME is unset.
    label='label=com.docker.compose.project.working_dir='+str(ROOT)
    dbs=docker('ps','-aq','--filter',label,'--filter','label=com.docker.compose.service=db',capture_output=True,text=True).stdout.split()
    if len(dbs)!=1:raise Stop('Cannot identify one existing local Compose database; review container IDs manually')
    info=json.loads(docker('inspect',dbs[0],capture_output=True,text=True).stdout)[0]
    project=info['Config']['Labels']['com.docker.compose.project']
    ids=[]
    for service in ('app','backup','caddy'):
        ids+=docker('ps','-aq','--filter',label,'--filter','label=com.docker.compose.project='+project,
                    '--filter','label=com.docker.compose.service='+service,capture_output=True,text=True).stdout.split()
    if ids:docker('stop',*ids,capture_output=True)
    print('Stopped only the identified local app/backup/Caddy; database container retained')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('check-source','build','export','verify','trial','compare','prepare-live','preflight','update','trial-app','start-live','stop','external-copy','pack-files','verify-files','external-copy-files'))
    parser.add_argument('--bundle')
    parser.add_argument('--database')
    args=parser.parse_args()
    try:
        if args.action=='check-source':source_request('check-source')
        elif args.action=='build':
            docker('build','-f','deploy/migration/Dockerfile','-t','grit-migration:ops','.',capture_output=True)
        elif args.action=='export': export_external(args.bundle or input('NEW bundle path: ').strip())
        elif args.action=='verify': verify_local(args.bundle or input('Bundle path: ').strip())
        elif args.action=='pack-files':
            pack_files(input('Directory of ORIGINAL files to archive: ').strip(),input('NEW protected file bundle directory (outside source): ').strip())
        elif args.action=='verify-files':verify_files(args.bundle or input('File bundle path: ').strip())
        elif args.action in ('external-copy','external-copy-files'):
            path=bundle_path(args.bundle or input('Verified bundle path: ').strip())
            (verify_files if args.action=='external-copy-files' else verify_local)(path)
            private_file(ROOT/'.env.backup')
            compose('run','--rm','--no-deps','--user',str(os.getuid())+':'+str(os.getgid()),'-v',str(path)+':/bundle','--entrypoint','python3','backup','/scripts/copy_bundle_s3.py',capture_output=True)
            print('PASS: external S3 copy verified; receipt saved inside the bundle')
        elif args.action=='prepare-live': prepare_live(args.database or input('NEW restored database name: ').strip())
        elif args.action=='preflight': preflight()
        elif args.action=='update': update()
        elif args.action=='stop':
            stop_local()
        else:
            bundle=args.bundle or input('Verified bundle path: ').strip()
            database=args.database or input('NEW/restored database name: ').strip()
            if args.action=='trial': operations('trial-restore',bundle,database)
            elif args.action=='compare': operations('compare-live',bundle,database)
            else: staged_start(bundle,database,live=args.action=='start-live')
        print('Command completed. Follow the next checkpoint in docs/HOSTKEY_FINAL_MIGRATION.md.')
    except Stop as error:
        raise SystemExit('STOP: '+str(error)) from None
    except (ValueError,OSError,KeyError,json.JSONDecodeError):
        # No tracebacks or subprocess output containing config/DSNs/record values.
        raise SystemExit('STOP: migration step failed or was refused. Existing databases/archives are retained. Check the step prerequisites in docs/HOSTKEY_FINAL_MIGRATION.md; do not continue.') from None


if __name__=='__main__': main()
