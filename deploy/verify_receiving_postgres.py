"""Run concurrency tests against a NEW local PostgreSQL in tmpfs, without host ports."""
import os
from pathlib import Path
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
ENV = {k:v for k,v in os.environ.items() if k not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}


def docker(*args, **kwargs):
    return subprocess.run(['docker','--host=unix:///var/run/docker.sock',*args], env=ENV, check=True, text=True, **kwargs)


def main():
    name='grit-receiving-test-'+uuid.uuid4().hex[:10]
    db=None
    docker('network','create','--internal',name,capture_output=True)
    try:
        db=docker('run','-d','--network',name,'--network-alias','db',
                  '--tmpfs','/var/lib/postgresql/data:rw',
                  '-e','POSTGRES_USER=grit','-e','POSTGRES_DB=grit',
                  '-e','POSTGRES_PASSWORD=isolated-test-only', 'postgres:17-bookworm',capture_output=True).stdout.strip()
        for attempt in range(30):
            probe=subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',db,'pg_isready','-U','grit','-d','grit'],env=ENV,capture_output=True)
            if probe.returncode==0: break
            time.sleep(1)
        else: raise RuntimeError('Disposable PostgreSQL did not start')
        docker('run','--rm','--network',name,'--user','0','--entrypoint','python',
               '-v',str(ROOT/'tests')+':/test:ro',
               '-e','GRIT_RECEIVING_TEST_DSN=postgresql://grit:isolated-test-only@db:5432/grit',
               '-e','PYTHONDONTWRITEBYTECODE=1','grit-wms:receiving-test',
               '-m','unittest','discover','-s','/test','-p','test_receiving_concurrency.py','-v')
    finally:
        if db: docker('rm','-f',db,capture_output=True)
        docker('network','rm',name,capture_output=True)


if __name__=='__main__': main()
