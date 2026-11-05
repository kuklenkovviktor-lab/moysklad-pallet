"""Isolated Docker integration test. Never uses .env, HOSTKEY or production data.

Requires locally built app/backup images and postgres:17-bookworm.
Optional --s3 requires grit-s3-test:local (tests/deployment/s3.Dockerfile).
Creates a unique project and temporary data directory; stops only its containers.
Uses only the local Unix Docker socket.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from init_env import generate

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    environment = dict(kwargs.pop('env', os.environ))
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    if command[0] == 'docker' and not command[1].startswith('--host='):
        command = ['docker', '--host=unix:///var/run/docker.sock'] + command[1:]
    return subprocess.run(command, check=True, text=True, env=environment, **kwargs)


def main():
    # Bind mounts require this directory to exist on the local Docker host.
    data = Path(tempfile.mkdtemp(prefix='grit-verify-', dir='/tmp'))
    (data / 'postgres').mkdir()
    (data / 'backups').mkdir()
    generate(data / '.env')
    s3_test = '--s3' in sys.argv
    emulator = None
    project = 'grit-verify-' + uuid.uuid4().hex[:10]
    environment = dict(os.environ, GRIT_DATA_ROOT=str(data), GRIT_PORT='0', BACKUP_S3_ENABLED='0', BACKUP_ALERT_REQUIRED='0')
    # Ambient shell variables cannot override these independently generated keys.
    for key in ('POSTGRES_PASSWORD', 'SECRET_KEY', 'WMS_BADGE_SECRET', 'WMS_TERMINAL_SECRET', 'MANAGER_WEB_PASSWORD'):
        environment.pop(key, None)
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    compose = ['docker', '--host=unix:///var/run/docker.sock', 'compose', '--project-directory', str(ROOT), '--env-file', str(data / '.env'), '-p', project, '-f', str(ROOT / 'compose.yaml')]

    safe_override = data / 'safe.yaml'
    fast_health = {'interval': '2s', 'start_period': '10s', 'retries': 10}
    safe_override.write_text(json.dumps({'services': {
        'app': {'healthcheck': fast_health}, 'db': {'healthcheck': fast_health},
        'backup': {'healthcheck': fast_health, 'environment': {
            'BACKUP_S3_ACCESS_KEY_ID': 'testing', 'BACKUP_S3_SECRET_ACCESS_KEY': 'testing',
            'BACKUP_ALERT_WEBHOOK_URL': '', 'BACKUP_ALERT_TELEGRAM_TOKEN': '',
            'BACKUP_ALERT_TELEGRAM_CHAT_ID': ''}}}}))
    compose += ['-f', str(safe_override)]
    if s3_test:
        tls = data / 'tls'
        tls.mkdir()
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-keyout', str(tls / 'key.pem'), '-out', str(tls / 'cert.pem'),
             '-subj', '/CN=s3-test', '-addext', 'subjectAltName=DNS:s3-test'], capture_output=True)
        (tls / 'cert.pem').chmod(0o644)
        override = data / 's3.yaml'
        override.write_text('services:\n  backup:\n    environment:\n'
                            '      BACKUP_S3_ENABLED: "1"\n'
                            '      BACKUP_S3_ENDPOINT: https://s3-test:5000\n'
                            '      BACKUP_S3_BUCKET: grit-isolated-tests\n'
                            '      BACKUP_S3_REGION: us-east-1\n'
                            '      BACKUP_S3_SSE: aws:kms\n'
                            '      BACKUP_S3_KMS_KEY_ID: test-key-id\n'
                            '      BACKUP_S3_ACCESS_KEY_ID: testing\n'
                            '      BACKUP_S3_SECRET_ACCESS_KEY: testing\n'
                            '      BACKUP_CA_BUNDLE: /test-ca.pem\n'
                            '      NO_PROXY: s3-test,db,localhost,127.0.0.1\n'
                            '      no_proxy: s3-test,db,localhost,127.0.0.1\n'
                            '    volumes:\n      - ' + str(tls / 'cert.pem') + ':/test-ca.pem:ro\n')
        compose += ['-f', str(override)]

    def dc(*args, **kwargs):
        return run(compose + list(args), env=environment, **kwargs)

    def sql(query, database='grit'):
        return dc('exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1', '-U', 'grit', '-d', database, '-Atc', query, capture_output=True).stdout.strip()

    def ready():
        for _ in range(90):
            ids = dc('ps', '-q', 'app', 'db', 'backup', capture_output=True).stdout.split()
            states = run(['docker', 'inspect', '--format', '{{.State.Health.Status}}'] + ids, capture_output=True).stdout.split() if len(ids) == 3 else []
            if states == ['healthy'] * 3:
                return
            time.sleep(2)
        raise RuntimeError('Stack did not become healthy')

    try:
        # Backup service drops all capabilities; give its UID 0 ownership of its
        # dedicated directory before startup. Never changes any existing paths.
        run(['docker', 'run', '--rm', '--network', 'none', '-v', f'{data / "backups"}:/backups', 'postgres:17-bookworm', 'sh', '-c', 'chown 0:0 /backups && chmod 700 /backups'])
        dc('config', '--quiet')
        if s3_test:
            dc('create')
            emulator = run(['docker', 'run', '-d', '--network', project + '_web',
                            '--network-alias', 's3-test', '-e', 'NO_PROXY=s3-test,localhost,127.0.0.1',
                            '-e', 'no_proxy=s3-test,localhost,127.0.0.1',
                            '-v', f'{data / "tls"}:/tls:ro', 'grit-s3-test:local'], capture_output=True).stdout.strip()
            for attempt in range(30):
                try:
                    run(['docker', 'exec', emulator, 'python', '-c',
                         "import boto3; from botocore.config import Config;  boto3.client('s3',endpoint_url='https://s3-test:5000',verify='/tls/cert.pem',aws_access_key_id='testing',aws_secret_access_key='testing',config=Config(connect_timeout=2,read_timeout=2,retries={'max_attempts':0})).create_bucket(Bucket='grit-isolated-tests')"], capture_output=True)
                    break
                except subprocess.CalledProcessError:
                    if attempt == 29:
                        raise
                    time.sleep(1)
        config = json.loads(dc('config', '--format', 'json', capture_output=True).stdout)
        assert 'ports' not in config['services']['db']
        assert config['services']['app']['ports'][0]['host_ip'] == '127.0.0.1'
        assert not config['services']['app']['environment']['MOYSKLAD_TOKEN']
        assert config['networks']['database']['internal']
        dc('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
        ready()
        dc('exec', '-T', 'app', 'python', '-c', "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/login'); assert r.status==200 and b'/api/auth/login' in r.read()")
        assert int(sql('SELECT count(*) FROM employees')) > 0
        sql('CREATE TABLE deployment_probe (value text); INSERT INTO deployment_probe VALUES (\'retained\')')
        dc('stop', 'backup')
        # Even direct manual runs must reject an uninitialized database.
        sql('CREATE DATABASE empty_backup_probe')
        initial = dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls /backups/grit-*.dump', capture_output=True).stdout
        empty = subprocess.run(compose + ['run', '--rm', '--no-deps', '-e', 'PGDATABASE=empty_backup_probe', '-e', 'BACKUP_SCHEMA_WAIT_SECONDS=0', '--entrypoint', 'sh', 'backup', '/scripts/backup-once.sh'], env=environment, capture_output=True)
        assert empty.returncode != 0
        assert initial == dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls /backups/grit-*.dump', capture_output=True).stdout

        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/backup-once.sh')
        listing = dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls -t /backups/grit-*.dump', capture_output=True).stdout.splitlines()
        archive = listing[0]
        failed_backup = subprocess.run(compose + ['run', '--rm', '--no-deps', '-e', 'PGHOST=127.0.0.1', '-e', 'PGCONNECT_TIMEOUT=2', '-e', 'BACKUP_SCHEMA_WAIT_SECONDS=0', '--entrypoint', 'sh', 'backup', '/scripts/backup-once.sh'], env=environment, capture_output=True)
        assert failed_backup.returncode != 0
        after_failure = dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls -t /backups/grit-*.dump', capture_output=True).stdout.splitlines()
        assert listing == after_failure, 'Failed backup changed retained archives'
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'test -z "$(find /backups -name "*.partial" -print)"')
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', archive, 'restore_check_test')
        assert sql('SELECT value FROM deployment_probe', 'restore_check_test') == 'retained'
        assert sql('SELECT value FROM deployment_probe') == 'retained'
        if s3_test:
            # Recover the actual uploaded object, check SHA-256 again and restore
            # into another independent DB, not the application's working DB.
            receipt = json.loads(dc('run', '--rm', '--no-deps', '--entrypoint', 'cat', 'backup', archive + '.uploaded.json', capture_output=True).stdout)
            dc('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'backup', '/scripts/fetch_s3.py', receipt['key'], 's3-recovered-test.dump', receipt['sha256'])
            dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', '/backups/s3-recovered-test.dump', 'restore_check_s3')
            assert sql('SELECT value FROM deployment_probe', 'restore_check_s3') == 'retained'
            # Real network failure of ONLY the disposable emulator: preserve
            # its process/data, and retry the same persistent pending queue.
            run(['docker','network','disconnect',project + '_web',emulator],capture_output=True)
            try:
                unavailable = subprocess.run(compose + ['run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-once.sh'],env=environment,capture_output=True)
                assert unavailable.returncode != 0
                failed_health = subprocess.run(compose + ['run','--rm','--no-deps','--entrypoint','sh','backup','/scripts/backup-health.sh'],env=environment,capture_output=True)
                assert failed_health.returncode != 0
                pending = dc('run','--rm','--no-deps','--entrypoint','sh','backup','-c','ls /backups/grit-*.dump.pending.json',capture_output=True).stdout
                assert pending.strip()
                before_retry = dc('run','--rm','--no-deps','--entrypoint','sh','backup','-c','ls /backups/grit-*.dump',capture_output=True).stdout
            finally:
                run(['docker','network','connect','--alias','s3-test',project + '_web',emulator],capture_output=True)
            dc('run','--rm','--no-deps','--entrypoint','python3','backup','-c',"import sys;sys.path.insert(0,'/scripts');import backup_worker as w;w.cycle(False);assert w.health()")
            after_retry = dc('run','--rm','--no-deps','--entrypoint','sh','backup','-c','ls /backups/grit-*.dump',capture_output=True).stdout
            assert before_retry == after_retry, 'Retry created another local dump instead of uploading the queue'
            print('PASS: actual S3 network outage retains archive/queue, fails health and recovers without another local dump')
        # Existing targets and corrupt archives must fail, without overwriting.
        repeat = subprocess.run(compose + ['run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', archive, 'restore_check_test'], env=environment, capture_output=True)
        assert repeat.returncode != 0
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'printf invalid > /backups/corrupt.dump')
        corrupt = subprocess.run(compose + ['run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', '/backups/corrupt.dump', 'restore_check_corrupt'], env=environment, capture_output=True)
        assert corrupt.returncode != 0
        assert sql("SELECT count(*) FROM pg_database WHERE datname='restore_check_corrupt'") == '0'
        dc('start', 'backup')
        dc('restart', 'db', 'app')
        ready()
        assert sql('SELECT value FROM deployment_probe') == 'retained'
        # Recreate containers, rather than only restarting processes: bind data
        # must survive removal of the original DB and application containers.
        dc('stop', 'backup', 'app', 'db')
        dc('rm', '-f', 'backup', 'app', 'db')
        dc('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
        ready()
        assert sql('SELECT value FROM deployment_probe') == 'retained'
        print('PASS: isolated startup, DB/app/backup health, HTTP login, no DB port, empty API token, backup/restore, corrupt/existing-target refusal and persistence after container recreation.')
    finally:
        if emulator:
            run(['docker', 'rm', '-f', emulator], capture_output=True)
        dc('down', '--remove-orphans')
        # Keep test data on failure for diagnosis. No destructive host rm, and no
        # production directory is ever used by this test.
        print(f'Isolated test artifacts retained at {data}; project {project} stopped.')


if __name__ == '__main__':
    main()
