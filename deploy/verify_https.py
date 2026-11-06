"""Actual TLS/proxy smoke on a private Docker network with a TEST CA, no public ACME."""
import os
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
ENV={k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args,**kwargs):
    return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args],env=ENV,check=True,text=True,**kwargs)


def main():
    folder=Path(tempfile.mkdtemp(prefix='grit-https-',dir='/tmp'))
    name='grit-https-'+uuid.uuid4().hex[:10];containers=[]
    config=(ROOT/'deploy/https/Caddyfile').read_text().replace('gritwms.space {','https://grit-proxy-test {\n    tls internal')
    path=folder/'Caddyfile';path.write_text(config)
    docker('network','create','--internal',name,capture_output=True)
    try:
        server='''from http.server import BaseHTTPRequestHandler,HTTPServer
import json
class Handler(BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Set-Cookie','session=test; Secure; HttpOnly; SameSite=Lax');self.end_headers();self.wfile.write(json.dumps({'xff':self.headers.get('X-Forwarded-For'),'scheme':self.headers.get('X-Forwarded-Proto')}).encode())
HTTPServer(('0.0.0.0',8000),Handler).serve_forever()
'''
        containers.append(docker('run','-d','--network',name,'--network-alias','app','--entrypoint','python','grit-wms:hostkey','-c',server,capture_output=True).stdout.strip())
        proxy=docker('run','-d','--network',name,'--network-alias','grit-proxy-test','--tmpfs','/data','--tmpfs','/config',
                     '-e','GRIT_ACME_EMAIL=night-test@example.invalid', '-e','NO_PROXY=app,localhost,127.0.0.1', '-e','no_proxy=app,localhost,127.0.0.1','-v',str(path)+':/etc/caddy/Caddyfile:ro','caddy:2-alpine',capture_output=True).stdout.strip()
        containers.append(proxy)
        for _ in range(30):
            try:
                certificate=docker('exec',proxy,'cat','/data/caddy/pki/authorities/local/root.crt',capture_output=True).stdout
                break
            except subprocess.CalledProcessError: time.sleep(1)
        else: raise RuntimeError('Test-only TLS CA did not become available')
        ca=folder/'test-ca.pem';ca.write_text(certificate);ca.chmod(0o644)
        code='''import requests
base='https://grit-proxy-test'
s=requests.Session();s.trust_env=False
r=s.get(base+'/login',verify='/test-ca.pem',headers={'X-Forwarded-For':'203.0.113.123'},timeout=5)
assert r.status_code==200,(r.status_code,r.text,dict(r.headers))
assert r.json()['scheme']=='https'
assert r.json()['xff']!='203.0.113.123',r.text
for key in ['Strict-Transport-Security','X-Content-Type-Options','X-Frame-Options','Content-Security-Policy','Permissions-Policy']:
 assert key in r.headers,key
assert 'camera=(self)' in r.headers['Permissions-Policy']
assert 'Secure' in r.headers['Set-Cookie']
for path in ['/api/health','/api/startup','/manager','/api/manager/fbs/skip-qr','/api/print-agent/test/next','/setup/test']:
 assert s.get(base+path,verify='/test-ca.pem',timeout=5).status_code==404,path
r=s.get('http://grit-proxy-test/login',allow_redirects=False,timeout=5)
assert r.status_code in (301,308) and r.headers['Location'].startswith('https://')
print('PASS: verified TLS, HTTPS redirect, mobile camera permission, Secure cookie, security headers, restricted operational routes and spoofed XFF replaced')
'''
        docker('run','--rm','--network',name,'--user','0','--entrypoint','python',
               '-v',str(ca)+':/test-ca.pem:ro','grit-wms:hostkey','-c',code)
    except Exception:
        for container in containers:
            docker('logs','--tail','15',container)
        raise
    finally:
        for container in reversed(containers): docker('rm','-f',container,capture_output=True)
        docker('network','rm',name,capture_output=True)
        print('Test TLS public artifacts retained at',folder)


if __name__=='__main__': main()
