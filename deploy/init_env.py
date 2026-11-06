"""Create local test secrets atomically without overwriting an existing .env."""
import os
from pathlib import Path
import secrets


def generate(path=Path('.env')):
    path = Path(path)
    values = {name: secrets.token_hex(32) for name in (
        'POSTGRES_PASSWORD', 'SECRET_KEY', 'WMS_BADGE_SECRET',
        'WMS_TERMINAL_SECRET', 'MANAGER_WEB_PASSWORD')}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write('# Local isolated test installation. Never commit this file.\n')
        for name, value in values.items():
            output.write(f'{name}={value}\n')
        output.write('GRIT_DATA_ROOT=/opt/grit-data\nGRIT_PORT=8000\n')
        output.write('BACKUP_INTERVAL_SECONDS=86400\nBACKUP_S3_ENABLED=0\nBACKUP_ALERT_REQUIRED=0\n')


if __name__ == '__main__':
    generate()
    print('Created .env (0600); secrets were not printed. Existing files are never overwritten.')
