"""Isolated Docker integration test. Never uses .env, HOSTKEY or production data.

Requires a locally built grit-wms:hostkey image and postgres:17-bookworm.
Creates a unique project and temporary data directory; stops only its containers.
Uses only the local Unix Docker socket.
"""
import json
import os
from pathlib import Path
import subprocess
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
    project = 'grit-verify-' + uuid.uuid4().hex[:10]
    environment = dict(os.environ, GRIT_DATA_ROOT=str(data), GRIT_PORT='0')
    # Ambient shell variables cannot override these independently generated keys.
    for key in ('POSTGRES_PASSWORD', 'SECRET_KEY', 'WMS_BADGE_SECRET', 'WMS_TERMINAL_SECRET', 'MANAGER_WEB_PASSWORD'):
        environment.pop(key, None)
    for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(key, None)
    compose = ['docker', '--host=unix:///var/run/docker.sock', 'compose', '--project-directory', str(ROOT), '--env-file', str(data / '.env'), '-p', project, '-f', str(ROOT / 'compose.yaml')]

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
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/backup-once.sh')
        listing = dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls -t /backups/grit-*.dump', capture_output=True).stdout.splitlines()
        archive = listing[0]
        failed_backup = subprocess.run(compose + ['run', '--rm', '--no-deps', '-e', 'PGHOST=127.0.0.1', '-e', 'PGCONNECT_TIMEOUT=2', '--entrypoint', 'sh', 'backup', '/scripts/backup-once.sh'], env=environment, capture_output=True)
        assert failed_backup.returncode != 0
        after_failure = dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'ls -t /backups/grit-*.dump', capture_output=True).stdout.splitlines()
        assert listing == after_failure, 'Failed backup changed retained archives'
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'test -z "$(find /backups -name "*.partial" -print)"')
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', archive, 'restore_check_test')
        assert sql('SELECT value FROM deployment_probe', 'restore_check_test') == 'retained'
        assert sql('SELECT value FROM deployment_probe') == 'retained'
        # Existing targets and corrupt archives must fail, without overwriting.
        repeat = subprocess.run(compose + ['run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', archive, 'restore_check_test'], env=environment, capture_output=True)
        assert repeat.returncode != 0
        dc('run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '-c', 'printf invalid > /backups/corrupt.dump')
        corrupt = subprocess.run(compose + ['run', '--rm', '--no-deps', '--entrypoint', 'sh', 'backup', '/scripts/restore.sh', '/backups/corrupt.dump', 'restore_check_corrupt'], env=environment, capture_output=True)
        assert corrupt.returncode != 0
        assert sql("SELECT count(*) FROM pg_database WHERE datname='restore_check_corrupt'") == '0'
        # Recreate containers, rather than only restarting processes: bind data
        # must survive removal of the original DB and application containers.
        dc('down')
        dc('up', '-d', '--no-build', '--wait', '--wait-timeout', '180')
        ready()
        assert sql('SELECT value FROM deployment_probe') == 'retained'
        print('PASS: isolated startup, DB/app/backup health, HTTP login, no DB port, empty API token, backup/restore, corrupt/existing-target refusal and persistence after container recreation.')
    finally:
        dc('down', '--remove-orphans')
        # Keep test data on failure for diagnosis. No destructive host rm, and no
        # production directory is ever used by this test.
        print(f'Isolated test artifacts retained at {data}; project {project} stopped.')


if __name__ == '__main__':
    main()
