#!/usr/bin/env python3
"""One operator command: isolated demo upgrade, authenticated preparation, binding.

Only /tmp/grit-pr208-demo.state. Never sources shell files or contacts production.
Keeps previous demo container stopped for rollback; never deletes containers/DB.
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/tmp/grit-pr208-demo.state')
PRIVATE = Path('/root/.config/grit-usb-bridge')
URL = 'http://127.0.0.1:18769'


def docker(*args):
    r = subprocess.run(['docker',*args],capture_output=True,text=True,check=False)
    if r.returncode:
        raise RuntimeError('Docker step failed (details suppressed to protect credentials)')
    return r.stdout.strip()


def private_write(path, data):
    if path.is_symlink():
        raise RuntimeError('Symlink refused')
    import tempfile
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def inspect_demo():
    if STATE.is_symlink() or STATE.stat().st_mode & 0o077:
        raise RuntimeError('Unsafe demo state permissions')
    values = dict(line.split('=',1) for line in STATE.read_text().splitlines())
    if set(values)!={'APP','DB','NET','PORT'} or values['PORT']!='18769':
        raise RuntimeError('Unexpected demo state')
    for key,prefix in [('APP','grit-ui-pr192demo-'),('DB','grit-db-pr192demo-'),('NET','grit-pr192demo-')]:
        if not re.fullmatch(re.escape(prefix)+r'[a-z0-9-]+',values[key]):
            raise RuntimeError('Non-demo identity refused')
    app, db = (json.loads(docker('inspect',values[k]))[0] for k in ('APP','DB'))
    env = dict(x.split('=',1) for x in app['Config']['Env'])
    pgname = next(x.split('=',1)[1] for x in db['Config']['Env'] if x.startswith('POSTGRES_DB='))
    if (env.get('DATABASE_URL') or env.get('GRIT_SYNTHETIC_PG_TEST')!='YES_ISOLATED' or
        not re.fullmatch(r'grit_pr192_synthetic_[a-z0-9_]+',pgname) or
        env.get('PR192_SYNTHETIC_DATABASE_URL')!='postgresql://postgres:synthetic-only@pr192-db:5432/'+pgname or
        db['Config']['Image']!='postgres:17'):
        raise RuntimeError('Non-isolated database refused')
    for obj in (app,db):
        if set(obj['NetworkSettings']['Networks'])!={values['NET']}:
            raise RuntimeError('Unexpected network attachment')
    network=json.loads(docker('network','inspect',values['NET']))[0]
    if {x['Name'] for x in network['Containers'].values()}!={values['APP'],values['DB']}:
        raise RuntimeError('Shared network refused')
    if app['HostConfig']['PortBindings']!={'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'18769'}]}:
        raise RuntimeError('Unexpected port binding')
    version=docker('exec',values['DB'],'psql','-U','postgres','-d',pgname,'-Atc','SHOW server_version_num')
    if int(version)//10000!=17:
        raise RuntimeError('PostgreSQL 17 required')
    return values,app,env,pgname


def upgrade(values, app, env):
    """Only isolated app swap, same disposable DB. Old container is retained."""
    new = 'grit-ui-pr192demo-usb-'+uuid.uuid4().hex[:12]
    env.update(GRIT_PR192_USB_AGENT_TOKEN=secrets.token_urlsafe(40),
               GRIT_PR192_DEMO_PASSWORD=secrets.token_urlsafe(32),
               GRIT_PR192_DEMO_SECRET=secrets.token_urlsafe(40))
    envfile = PRIVATE/'demo.env'
    private_write(envfile,'\n'.join(k+'='+v for k,v in env.items())+'\n')
    old=values['APP']
    private_write(PRIVATE/'rollback.json',json.dumps({'old_app':old,'new_app':new,'state':values}))
    docker('stop',old)
    try:
        docker('run','-d','--name',new,'--network',values['NET'],'--no-healthcheck',
               '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true',
               '--tmpfs','/tmp:rw,size=128m','-p','127.0.0.1:18769:8000',
               '-v',str(ROOT)+':/app:ro','-w','/app','--env-file',str(envfile),
               '--entrypoint','python',app['Image'],'-m','gunicorn','--bind','0.0.0.0:8000',
               '--workers','1','--timeout','60','line_printer_demo_app:app')
        probe=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(30):
            try:
                with probe.open(URL+'/health',timeout=2) as response:
                    if json.load(response)=={'ok':True,'isolation':'synthetic','production':False}:
                        break
            except Exception:
                pass
            time.sleep(1)
        else:
            raise RuntimeError('Isolated replacement unhealthy')
        values=dict(values,APP=new)
        private_write(STATE,''.join(k+'='+v+'\n' for k,v in values.items()))
    except Exception:
        subprocess.run(['docker','stop',new],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        docker('start',old)
        raise RuntimeError('FAILED: old isolated demo restored; production untouched') from None
    finally:
        envfile.unlink(missing_ok=True)
    return values,env


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):
        return None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--printer-id',required=True)
    parser.add_argument('--approve-one',action='store_true',required=True,
                        help='Explicit approval by test supervisor demo-manager for ONE invented label')
    parser.add_argument('--refresh-demo',action='store_true',help='Upgrade isolated app only and rotate exposed demo secrets')
    args=parser.parse_args()
    if os.geteuid()!=0 or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',args.printer_id):
        raise RuntimeError('Root test supervisor and exact printer ID required')
    if PRIVATE.is_symlink():
        raise RuntimeError('Unsafe private directory')
    PRIVATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    os.chmod(PRIVATE,0o700)
    import fcntl
    lock=open(PRIVATE/'prepare.lock','a')
    os.chmod(PRIVATE/'prepare.lock',0o600)
    fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
    values,app,env,pgname=inspect_demo()
    if args.refresh_demo:
        values,env=upgrade(values,app,env)
    # Durable request ID BEFORE HTTP: rerunning never creates another attempt.
    request_path=PRIVATE/('attempt-'+hashlib.sha256((pgname+args.printer_id).encode()).hexdigest()+'.json')
    if request_path.exists():
        attempt=json.loads(request_path.read_text())
    else:
        attempt={'request_id':uuid.uuid4().hex,'printer_id':args.printer_id}
        private_write(request_path,json.dumps(attempt))
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect,
                                      urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def post(path,data):
        req=urllib.request.Request(URL+path,data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=10) as response:
            return json.load(response)
    login=post('/demo/login',{'mode':'manager','password':env['GRIT_PR192_DEMO_PASSWORD']})
    if login.get('manager') is not True:
        raise RuntimeError('Test supervisor authorization failed')
    with opener.open(URL+'/demo/session',timeout=5) as response:
        auth=json.load(response)
    result=post('/demo/printing/usb/prepare',dict(attempt,csrf=auth['manager_csrf']))
    if result.get('synthetic') is not True or result.get('physical_printing') is not False:
        raise RuntimeError('Invalid synthetic response')
    if result['state']=='READY':
        private_write(PRIVATE/'enrollment.json',json.dumps({
            'url':URL,'printer':'ONIX-TEST-XP460B','token':env['GRIT_PR192_USB_AGENT_TOKEN'],
            'stand_id':result['stand_id'],'ticket_id':result['ticket_id']}))
    print(result['state']+': '+result['order_id']+'; printer_id='+args.printer_id+'; NO PRINT')


if __name__=='__main__':
    try:
        main()
    except Exception:
        print('FAILED: isolated preparation refused; no secrets logged. Check stand, exact printer ID and active approval.',file=sys.stderr)
        sys.exit(1)
