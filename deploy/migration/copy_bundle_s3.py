"""Copy a complete verified migration bundle to unique, conditional S3 objects.

Uses existing backup credentials/TLS configuration. Never deletes stored objects.
A receipt is committed only after HEAD and FULL GET SHA-256 for every file.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from backup_worker import s3_client, digest
from botocore.exceptions import ClientError

PART=64*1024*1024


def upload_object(client,bucket,key,path,encryption):
    expected=digest(path);size=path.stat().st_size
    try:
        client.head_object(Bucket=bucket,Key=key)
    except ClientError as error:
        if str(error.response.get('Error',{}).get('Code')) not in ('404','NoSuchKey','NotFound'):raise
    else:
        raise RuntimeError('Existing S3 object refused')
    # Multipart completion has an atomic create-only precondition, unlike a
    # racy HEAD-before-upload check. Every key also uses a new random bundle ID.
    created=client.create_multipart_upload(Bucket=bucket,Key=key,Metadata={'sha256':expected},**encryption)
    upload_id=created['UploadId'];completed=False
    try:
        parts=[]
        with path.open('rb') as stream:
            for number in range(1,10001):
                data=stream.read(PART)
                if not data:break
                part=client.upload_part(Bucket=bucket,Key=key,UploadId=upload_id,PartNumber=number,Body=data)
                parts.append({'PartNumber':number,'ETag':part['ETag']})
            if stream.read(1):raise RuntimeError('Archive exceeds multipart limit')
        if not parts:raise RuntimeError('Empty file refused')
        response=client.complete_multipart_upload(Bucket=bucket,Key=key,UploadId=upload_id,
            MultipartUpload={'Parts':parts},IfNoneMatch='*')
        completed=True
        version=response.get('VersionId')
        if version=='null':version=None  # Unversioned S3 providers may return literal "null".
        address={'Bucket':bucket,'Key':key}
        if version:address['VersionId']=version
        head=client.head_object(**address)
        if head['ContentLength']!=size or head.get('Metadata',{}).get('sha256')!=expected:
            raise RuntimeError('Remote metadata mismatch')
        body=client.get_object(**address)['Body'];value=hashlib.sha256();received=0
        try:
            for chunk in iter(lambda:body.read(1024*1024),b''):
                value.update(chunk);received+=len(chunk)
        finally:body.close()
        if received!=size or value.hexdigest()!=expected or digest(path)!=expected:
            raise RuntimeError('Full verification failed')
        return {'key':key,'version_id':version,'size':size,'sha256':expected}
    finally:
        if not completed:
            # Only this invocation's incomplete upload; no existing object.
            client.abort_multipart_upload(Bucket=bucket,Key=key,UploadId=upload_id)


def copy_bundle(folder):
    expected=json.loads((folder/'checksums.json').read_text())
    if set(expected) not in ({'database.dump','manifest.json'},{'files.tar.gz','files-manifest.json'}):raise RuntimeError('Invalid checksums')
    files=tuple(sorted(expected))+('checksums.json',)
    for name in files:
        if (folder/name).is_symlink() or not (folder/name).is_file():raise RuntimeError('Invalid file')
    if any(digest(folder/name)!=value for name,value in expected.items()):raise RuntimeError('Local checksum mismatch')
    encryption={'ServerSideEncryption':os.getenv('BACKUP_S3_SSE','AES256')}
    if encryption['ServerSideEncryption']=='aws:kms':
        encryption['SSEKMSKeyId']=os.environ['BACKUP_S3_KMS_KEY_ID']
    elif encryption['ServerSideEncryption']!='AES256':raise RuntimeError('S3 encryption required')
    prefix=os.environ.get('BACKUP_S3_PREFIX','grit-wms').strip('/')
    if not prefix or '..' in prefix.split('/'):raise RuntimeError('Invalid prefix')
    identity=uuid.uuid4().hex;prefix+='/migration/'+identity
    client=s3_client();bucket=os.environ['BACKUP_S3_BUCKET']
    receipt={'endpoint':os.environ['BACKUP_S3_ENDPOINT'],'bucket':bucket,'objects':[]}
    for name in files:
        receipt['objects'].append(upload_object(client,bucket,prefix+'/'+name,folder/name,encryption))
    fd=os.open(folder/('external-copy-'+identity+'.json'),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(receipt,stream,sort_keys=True,indent=2);stream.flush();os.fsync(stream.fileno())
    return receipt


if __name__=='__main__':
    try:
        copy_bundle(Path('/bundle'))
        print('PASS: independent S3 copy of all three bundle files, full GET SHA-256 verified; receipt saved')
    except Exception:
        raise SystemExit('S3 copy failed: check endpoint, CA, credentials, conditional multipart permissions and network. Do not continue cutover. Existing objects retained; retry creates a NEW prefix.') from None
