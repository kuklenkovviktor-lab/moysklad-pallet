"""Create an empty, private template. Do not generate or request real S3 keys."""
import os
from pathlib import Path


def generate(path=Path('.env.backup')):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('# Fill securely on HOSTKEY only. Never commit or share this file.\n')
        for name in ('BACKUP_S3_ACCESS_KEY_ID', 'BACKUP_S3_SECRET_ACCESS_KEY',
                     'BACKUP_ALERT_WEBHOOK_URL', 'BACKUP_ALERT_TELEGRAM_TOKEN',
                     'BACKUP_ALERT_TELEGRAM_CHAT_ID'):
            stream.write(name + '=\n')


if __name__ == '__main__':
    generate()
    print('Created private .env.backup template; existing files are never overwritten.')
