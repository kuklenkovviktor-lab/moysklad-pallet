"""Download and verify an S3 backup into a NEW local file; never restore a DB."""
import contextlib
import hashlib
import os
from pathlib import Path
import re
import sys
import tempfile

from backup_worker import ROOT, Failure, s3_client


def download(key, filename, expected_sha=None):
    if Path(filename).name != filename or not filename.endswith('.dump'):
        raise Failure('invalid_download_target')
    final = ROOT / filename
    if final.exists():
        raise Failure('download_target_exists')
    prefix = os.getenv('BACKUP_S3_PREFIX', 'grit-wms/hostkey-test').strip('/') + '/'
    if not key.startswith(prefix) or '..' in key.split('/'):
        raise Failure('invalid_download_key')
    client = s3_client()
    bucket = os.environ['BACKUP_S3_BUCKET']
    temporary = None
    try:
        head = client.head_object(Bucket=bucket, Key=key)
        sha = head.get('Metadata', {}).get('sha256', '')
        if not re.fullmatch('[0-9a-f]{64}', sha) or (expected_sha and expected_sha != sha):
            raise Failure('download_checksum_missing_or_changed')
        version = head.get('VersionId')
        response = client.get_object(Bucket=bucket, Key=key, **({'VersionId': version} if version else {}))
        result = hashlib.sha256()
        length = 0
        fd, temporary = tempfile.mkstemp(prefix='.s3-download-', suffix='.partial', dir=ROOT)
        with os.fdopen(fd, 'wb') as output, contextlib.closing(response['Body']) as body:
            for chunk in iter(lambda: body.read(1024 * 1024), b''):
                output.write(chunk)
                result.update(chunk)
                length += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if length != head['ContentLength'] or result.hexdigest() != sha:
            raise Failure('download_verification_failed')
        # Atomic exclusive publication: never replace an existing local backup.
        os.link(temporary, final)
        return final
    except Failure:
        raise
    except Exception:
        raise Failure('download_failed') from None
    finally:
        if temporary:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


if __name__ == '__main__':
    os.umask(0o077)
    if len(sys.argv) not in (3, 4):
        raise SystemExit('Usage: fetch_s3.py object-key new-name.dump [expected-sha256]')
    try:
        download(*sys.argv[1:])
        print('S3 backup downloaded and SHA-256 verified; no database changed')
    except Failure as error:
        raise SystemExit(str(error)) from None
