"""PostgreSQL backups with schema gating, persistent upload queue and read-back verification.

Never deletes archives. Errors/logs/notifications contain stages, never credentials.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid

import boto3
from botocore.config import Config
from boto3.s3.transfer import TransferConfig

ROOT = Path('/backups')
REQUIRED_TABLES = ('employees', 'shifts', 'scans', 'activities',
                   'operation_events', 'receiving_confirmations', 'fbs_catalog_barcodes')


class Failure(Exception):
    """Only a public, fixed stage string may be exposed outside this module."""


def enabled(name):
    value = os.getenv(name, '0').lower()
    if value not in ('0', 'false', 'no', '', '1', 'true', 'yes'):
        raise Failure('invalid_configuration')
    return value in ('1', 'true', 'yes')


def integer(name, default, minimum=1):
    try:
        value = int(os.getenv(name, str(default)))
        if value < minimum:
            raise ValueError()
        return value
    except ValueError:
        raise Failure('invalid_configuration') from None


def read_json(name):
    try:
        return json.loads((ROOT / name).read_text())
    except (OSError, ValueError):
        return {}


def atomic(name, value):
    fd, temporary = tempfile.mkstemp(prefix='.state-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, ROOT / name)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(part)
    return result.hexdigest()


def command(args, stage, **kwargs):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=integer('BACKUP_COMMAND_TIMEOUT_SECONDS', 900), **kwargs)
    except (subprocess.SubprocessError, OSError):
        raise Failure(stage) from None


def schema_ready():
    names = ','.join("to_regclass('public.%s') IS NOT NULL" % name for name in REQUIRED_TABLES)
    try:
        result = command(['psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-c',
                          'SELECT ' + ' AND '.join(names.split(','))], 'schema_not_ready', text=True)
        if result.stdout.strip() != 't':
            return False
        rows = command(['psql', '-X', '-At', '-v', 'ON_ERROR_STOP=1', '-c',
                        'SELECT count(*) FROM public.employees'], 'schema_not_ready', text=True)
        return int(rows.stdout.strip()) > 0
    except (Failure, ValueError):
        return False


def wait_for_schema():
    deadline = time.monotonic() + integer('BACKUP_SCHEMA_WAIT_SECONDS', 180, 0)
    while not schema_ready():
        if time.monotonic() >= deadline:
            raise Failure('schema_not_ready')
        time.sleep(min(2, max(0, deadline - time.monotonic())))


def create_archive():
    wait_for_schema()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    name = f'grit-{stamp}-{uuid.uuid4().hex}.dump'
    temporary = ROOT / ('.' + name + '.partial')
    final = ROOT / name
    try:
        command(['pg_dump', '--format=custom', '--no-owner', '--no-acl', '--file', str(temporary)], 'pg_dump_failed')
        toc = command(['pg_restore', '--list', str(temporary)], 'archive_invalid', text=True).stdout
        if not all(f'TABLE DATA public {name} ' in toc for name in REQUIRED_TABLES):
            raise Failure('archive_missing_schema')
        with temporary.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        atomic(name + '.pending.json', {'sha256': digest(final), 'size': final.stat().st_size})
        atomic('.last-local.json', {'time': time.time(), 'file': name})
        return final
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()  # Only our incomplete output; never an archive.


def s3_client():
    endpoint = os.getenv('BACKUP_S3_ENDPOINT', '')
    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Failure('s3_configuration')
    if not all(os.getenv(key) for key in ('BACKUP_S3_BUCKET', 'BACKUP_S3_ACCESS_KEY_ID', 'BACKUP_S3_SECRET_ACCESS_KEY')):
        raise Failure('s3_configuration')
    return boto3.client('s3', endpoint_url=endpoint,
                        region_name=os.getenv('BACKUP_S3_REGION', 'us-east-1'),
                        aws_access_key_id=os.environ['BACKUP_S3_ACCESS_KEY_ID'],
                        aws_secret_access_key=os.environ['BACKUP_S3_SECRET_ACCESS_KEY'],
                        verify=os.getenv('BACKUP_CA_BUNDLE') or True,
                        config=Config(connect_timeout=5, read_timeout=30,
                                      retries={'max_attempts': 3, 'mode': 'standard'},
                                      s3={'addressing_style': 'path'}))


def upload(path, expected):
    client = s3_client()
    bucket = os.environ['BACKUP_S3_BUCKET']
    prefix = os.getenv('BACKUP_S3_PREFIX', 'grit-wms/hostkey-test').strip('/')
    if not prefix or '..' in prefix.split('/'):
        raise Failure('s3_configuration')
    key = prefix + '/' + path.name
    # Bind a pending archive to its destination before uploading. Changing S3
    # settings cannot silently move an existing pending backup elsewhere.
    destination = {'endpoint': os.environ['BACKUP_S3_ENDPOINT'], 'bucket': bucket, 'key': key}
    if expected.get('destination') and expected['destination'] != destination:
        raise Failure('s3_destination_changed')
    expected['destination'] = destination
    atomic(path.name + '.pending.json', expected)
    extra = {'Metadata': {'sha256': expected['sha256']}}
    encryption = os.getenv('BACKUP_S3_SSE', 'AES256')
    if encryption:
        if encryption not in ('AES256', 'aws:kms'):
            raise Failure('s3_configuration')
        extra['ServerSideEncryption'] = encryption
        if encryption == 'aws:kms':
            key_id = os.getenv('BACKUP_S3_KMS_KEY_ID', '')
            if not key_id:
                raise Failure('s3_kms_configuration')
            extra['SSEKMSKeyId'] = key_id
    try:
        # Detect changed/corrupt local files before uploading them.
        if path.stat().st_size != expected['size'] or digest(path) != expected['sha256']:
            raise Failure('local_archive_changed')
        client.upload_file(str(path), bucket, key, ExtraArgs=extra,
                           Config=TransferConfig(max_concurrency=2))
    except Failure:
        raise
    except Exception:
        raise Failure('s3_upload_failed') from None
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        # S3-compatible providers may change the case of user metadata keys (e.g. Sha256).
        # Keep checking the expected checksum; normalize only the metadata key name.
        metadata = {str(name).lower(): value for name, value in head.get('Metadata', {}).items()}
        if head['ContentLength'] != expected['size'] or metadata.get('sha256') != expected['sha256']:
            raise Failure('s3_verification_failed')
        version = head.get('VersionId')
        options = {'VersionId': version} if version else {}
        response = client.get_object(Bucket=bucket, Key=key, **options)
        result = hashlib.sha256()
        size = 0
        with contextlib.closing(response['Body']) as body:
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                result.update(chunk)
                size += len(chunk)
        if size != expected['size'] or result.hexdigest() != expected['sha256']:
            raise Failure('s3_verification_failed')
    except Failure:
        raise
    except Exception:
        raise Failure('s3_verification_failed') from None
    receipt = dict(destination, sha256=expected['sha256'], size=expected['size'],
                   version_id=version, time=time.time())
    atomic(path.name + '.uploaded.json', receipt)
    (ROOT / (path.name + '.pending.json')).unlink()
    atomic('.last-s3.json', receipt)


def alert_configured():
    return bool(os.getenv('BACKUP_ALERT_WEBHOOK_URL') or
                (os.getenv('BACKUP_ALERT_TELEGRAM_TOKEN') and os.getenv('BACKUP_ALERT_TELEGRAM_CHAT_ID')))


def notify(stage, force=False):
    previous = read_json('.last-alert.json')
    repeat = integer('BACKUP_ALERT_REPEAT_SECONDS', 3600, 60)
    if not force and previous.get('stage') == stage and time.time() - previous.get('time', 0) < repeat:
        return True
    payload = {'event': 'grit_backup_failed', 'stage': stage,
               'time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    url = os.getenv('BACKUP_ALERT_WEBHOOK_URL', '')
    telegram = False
    if not url and os.getenv('BACKUP_ALERT_TELEGRAM_TOKEN'):
        telegram = True
        url = 'https://api.telegram.org/bot' + os.environ['BACKUP_ALERT_TELEGRAM_TOKEN'] + '/sendMessage'
        payload = {'chat_id': os.getenv('BACKUP_ALERT_TELEGRAM_CHAT_ID', ''),
                   'text': 'GRIT PostgreSQL backup failed: ' + stage}
    if not url or not url.startswith('https://'):
        print('Backup notification unavailable: configure an HTTPS webhook or Telegram', flush=True)
        return False
    try:
        context = ssl.create_default_context(cafile=os.getenv('BACKUP_CA_BUNDLE') or None)
        request = Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=10, context=context) as response:
            if not 200 <= response.status < 300:
                raise ValueError()
            if telegram and not json.loads(response.read(65536)).get('ok'):
                raise ValueError()
        atomic('.last-alert.json', {'stage': stage, 'time': time.time()})
        print('Backup failure notification delivered', flush=True)
        return True
    except Exception:
        # Do not log the URL, response body, token, bucket or exception text.
        print('Backup failure notification failed; will retry next cycle', flush=True)
        return False


def retry_notifications():
    pending = read_json('.pending-alerts.json')
    for stage in list(pending):
        try:
            if notify(stage):
                pending.pop(stage)
                atomic('.pending-alerts.json', pending)
        except Exception:
            print('Backup failure notification configuration invalid', flush=True)


def cycle(force_local=False):
    offsite = enabled('BACKUP_S3_ENABLED')
    interval = integer('BACKUP_INTERVAL_SECONDS', 86400, 60)
    latest = read_json('.last-local.json')
    if force_local or time.time() - latest.get('time', 0) >= interval:
        create_archive()
    if offsite:
        failure = None
        for pending in sorted(ROOT.glob('grit-*.dump.pending.json')):
            try:
                upload(ROOT / pending.name.removesuffix('.pending.json'), read_json(pending.name))
            except Exception as exc:
                failure = exc
        if failure:
            raise failure
    # A failure is cleared only by a complete cycle, including all pending uploads.
    atomic('.last-cycle.json', {'time': time.time(), 's3': enabled('BACKUP_S3_ENABLED')})
    with contextlib.suppress(FileNotFoundError):
        (ROOT / '.last-error.json').unlink()
    print('PostgreSQL backup cycle completed; S3=' + str(enabled('BACKUP_S3_ENABLED')), flush=True)


def health():
    if (ROOT / '.last-error.json').exists():
        return False
    latest = read_json('.last-local.json')
    interval = integer('BACKUP_INTERVAL_SECONDS', 86400, 60)
    age = time.time() - latest.get('time', 0)
    if not 0 <= age <= interval + 300 or not (ROOT / latest.get('file', '__missing__')).is_file():
        return False
    if enabled('BACKUP_ALERT_REQUIRED') and (not alert_configured() or read_json('.pending-alerts.json')):
        return False
    if enabled('BACKUP_S3_ENABLED'):
        if list(ROOT.glob('grit-*.dump.pending.json')):
            return False
        receipt = read_json(latest['file'] + '.uploaded.json')
        prefix = os.getenv('BACKUP_S3_PREFIX', 'grit-wms/hostkey-test').strip('/')
        return (receipt.get('endpoint') == os.getenv('BACKUP_S3_ENDPOINT')
                and receipt.get('bucket') == os.getenv('BACKUP_S3_BUCKET')
                and receipt.get('key') == prefix + '/' + latest['file']
                and 0 <= time.time() - receipt.get('time', 0) <= interval + 300)
    return True


def execute(force_local):
    try:
        cycle(force_local)
        # Deliver an earlier failure even if S3 recovered before Telegram did.
        retry_notifications()
        return True
    except Exception as exc:
        stage = str(exc) if isinstance(exc, Failure) else 'backup_internal_error'
        atomic('.last-error.json', {'time': time.time(), 'stage': stage})
        print('PostgreSQL backup failed: ' + stage, flush=True)
        pending = read_json('.pending-alerts.json')
        pending.setdefault(stage, time.time())
        atomic('.pending-alerts.json', pending)
        retry_notifications()
        return False


def main():
    os.umask(0o077)
    action = sys.argv[1] if len(sys.argv) > 1 else 'loop'
    if action == 'health':
        raise SystemExit(0 if health() else 1)
    # The persistent lock serializes manual runs with the scheduled worker.
    if action not in ('once', 'loop', 'notify-test'):
        raise SystemExit(2)
    if action == 'notify-test':
        raise SystemExit(0 if notify('notification_test', force=True) else 1)
    while True:
        with (ROOT / '.backup.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if action == 'once':
                    print('Backup already running', flush=True)
                    raise SystemExit(2)
            else:
                ok = execute(action == 'once')
                if action == 'once':
                    raise SystemExit(0 if ok else 1)
        time.sleep(60)  # retries pending S3 uploads without creating a dump each minute


if __name__ == '__main__':
    main()
