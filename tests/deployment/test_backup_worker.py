import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('backup_worker', ROOT / 'deploy/backup_worker.py')
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class FakeS3:
    def upload_file(self, path, bucket, key, **kwargs):
        self.data = Path(path).read_bytes()
        self.metadata = kwargs['ExtraArgs']['Metadata']
        self.extra = kwargs['ExtraArgs']

    def head_object(self, **kwargs):
        return {'ContentLength': len(self.data), 'Metadata': self.metadata, 'VersionId': 'test-version'}

    def get_object(self, **kwargs):
        return {'Body': io.BytesIO(self.data)}


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.patch_root = patch.object(w, 'ROOT', Path(self.folder.name))
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)
        self.env = patch.dict(os.environ, {'BACKUP_INTERVAL_SECONDS': '3600', 'BACKUP_S3_ENABLED': '1', 'BACKUP_S3_ENDPOINT': 'https://s3.example.test', 'BACKUP_S3_BUCKET': 'test-bucket', 'BACKUP_S3_PREFIX': 'isolated'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def archive(self):
        file = w.ROOT / 'grit-test.dump'
        file.write_bytes(b'only isolated test data')
        expected = {'sha256': w.digest(file), 'size': file.stat().st_size}
        w.atomic(file.name + '.pending.json', expected)
        w.atomic('.last-local.json', {'time': time.time(), 'file': file.name})
        return file, expected

    def test_no_dump_before_required_schema_and_seed(self):
        os.environ['BACKUP_SCHEMA_WAIT_SECONDS'] = '0'
        with patch.object(w, 'schema_ready', return_value=False), patch.object(w, 'command') as command:
            with self.assertRaisesRegex(w.Failure, 'schema_not_ready'):
                w.create_archive()
            command.assert_not_called()
        self.assertEqual(list(w.ROOT.glob('*.dump')), [])

    def test_schema_requires_seed_not_only_tables(self):
        result = type('Result', (), {})()
        result.stdout = 't\n'
        empty = type('Result', (), {})()
        empty.stdout = '0\n'
        with patch.object(w, 'command', side_effect=[result, empty]):
            self.assertFalse(w.schema_ready())

    def test_archive_without_core_table_data_rejected(self):
        with patch.object(w, 'wait_for_schema'), patch.object(w, 'command', return_value=type('Result', (), {'stdout': 'unrelated table'})()):
            with self.assertRaisesRegex(w.Failure, 'archive_missing_schema'):
                w.create_archive()
        self.assertFalse(list(w.ROOT.glob('*.dump')))

    def test_upload_requires_full_readback_before_receipt(self):
        file, expected = self.archive()
        with patch.object(w, 's3_client', return_value=FakeS3()):
            w.upload(file, expected)
        self.assertTrue(file.exists())
        self.assertFalse((w.ROOT / (file.name + '.pending.json')).exists())
        self.assertEqual(w.read_json('.last-s3.json')['sha256'], w.digest(file))
        self.assertTrue(w.health())

    def test_yandex_kms_options_are_explicit_and_key_is_required(self):
        file, expected = self.archive()
        client = FakeS3()
        os.environ['BACKUP_S3_SSE'] = 'aws:kms'
        with patch.object(w, 's3_client', return_value=client):
            with self.assertRaisesRegex(w.Failure, 's3_kms_configuration'):
                w.upload(file, expected)
            os.environ['BACKUP_S3_KMS_KEY_ID'] = 'test-key-id'
            w.upload(file, expected)
        self.assertEqual(client.extra['ServerSideEncryption'], 'aws:kms')
        self.assertEqual(client.extra['SSEKMSKeyId'], 'test-key-id')

    def test_content_mismatch_cannot_be_reported_successful(self):
        file, expected = self.archive()
        client = FakeS3()
        client.get_object = lambda **kwargs: {'Body': io.BytesIO(b'corrupt')}
        with patch.object(w, 's3_client', return_value=client):
            with self.assertRaisesRegex(w.Failure, 's3_verification_failed'):
                w.upload(file, expected)
        self.assertTrue(file.exists())
        self.assertTrue((w.ROOT / (file.name + '.pending.json')).exists())
        self.assertFalse((w.ROOT / '.last-s3.json').exists())
        self.assertFalse(w.health())

    def test_head_mismatch_cannot_pass_verification(self):
        file, expected = self.archive()
        client = FakeS3()
        client.head_object = lambda **kwargs: {'ContentLength': 1, 'Metadata': {}}
        with patch.object(w, 's3_client', return_value=client):
            with self.assertRaisesRegex(w.Failure, 's3_verification_failed'):
                w.upload(file, expected)

    def test_failed_upload_preserves_queue_and_retry_does_not_dump_again(self):
        file, expected = self.archive()
        client = FakeS3()
        client.upload_file = unittest.mock.Mock(side_effect=RuntimeError('secret-test-only'))
        with patch.object(w, 's3_client', return_value=client), patch.object(w, 'notify'), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertFalse(w.execute(False))
        self.assertNotIn('secret-test-only', output.getvalue())
        self.assertTrue(file.exists())
        self.assertTrue((w.ROOT / '.last-error.json').exists())
        with patch.object(w, 's3_client', return_value=FakeS3()), patch.object(w, 'create_archive') as create:
            self.assertTrue(w.execute(False))
            create.assert_not_called()
        self.assertFalse((w.ROOT / '.last-error.json').exists())
        self.assertTrue(w.health())

    def test_destination_change_blocks_existing_pending_upload(self):
        file, expected = self.archive()
        client = FakeS3()
        expected['destination'] = {'endpoint': 'https://other.example.test', 'bucket': 'other', 'key': 'elsewhere'}
        with patch.object(w, 's3_client', return_value=client):
            with self.assertRaisesRegex(w.Failure, 's3_destination_changed'):
                w.upload(file, expected)
        self.assertFalse(hasattr(client, 'data'))

    def test_changed_local_file_is_not_uploaded(self):
        file, expected = self.archive()
        file.write_bytes(b'changed')
        with patch.object(w, 's3_client', return_value=FakeS3()):
            with self.assertRaisesRegex(w.Failure, 'local_archive_changed'):
                w.upload(file, expected)

    def test_old_local_success_does_not_make_offsite_healthy(self):
        self.archive()
        self.assertFalse(w.health())

    def test_required_alert_channel_is_part_of_readiness(self):
        os.environ['BACKUP_S3_ENABLED'] = '0'
        os.environ['BACKUP_ALERT_REQUIRED'] = '1'
        self.archive()
        self.assertFalse(w.health())

    def test_webhook_failure_is_not_suppressed_and_never_logs_secret(self):
        os.environ['BACKUP_ALERT_WEBHOOK_URL'] = 'https://hooks.example.test/secret-test-only'
        with patch.object(w, 'urlopen', side_effect=RuntimeError('secret-test-only')), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertFalse(w.notify('s3_upload_failed'))
            self.assertFalse(w.notify('s3_upload_failed'))
        self.assertNotIn('secret-test-only', output.getvalue())
        self.assertFalse((w.ROOT / '.last-alert.json').exists())

    def test_delivered_webhook_is_rate_limited(self):
        os.environ['BACKUP_ALERT_WEBHOOK_URL'] = 'https://hooks.example.test/test-only'
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 204
        with patch.object(w, 'urlopen', return_value=response) as request:
            self.assertTrue(w.notify('s3_upload_failed'))
            self.assertTrue(w.notify('s3_upload_failed'))
            self.assertEqual(request.call_count, 1)
            self.assertIsNotNone(request.call_args.kwargs['context'])

    def test_telegram_api_failure_is_not_delivery(self):
        os.environ['BACKUP_ALERT_TELEGRAM_TOKEN'] = 'test-only-not-a-real-token'
        os.environ['BACKUP_ALERT_TELEGRAM_CHAT_ID'] = '123'
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"ok":false}'
        with patch.object(w, 'urlopen', return_value=response):
            self.assertFalse(w.notify('s3_upload_failed'))
        self.assertFalse((w.ROOT / '.last-alert.json').exists())

    def test_existing_archives_never_deleted(self):
        os.environ['BACKUP_S3_ENABLED'] = '0'
        file, _ = self.archive()
        os.utime(file, (0, 0))
        w.cycle()
        self.assertTrue(file.exists())

    def fetch_module(self):
        spec = importlib.util.spec_from_file_location('fetch_s3', ROOT / 'deploy/fetch_s3.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'backup_worker': w}):
            spec.loader.exec_module(module)
        return module

    def test_s3_download_verified_and_never_overwrites(self):
        file, expected = self.archive()
        client = FakeS3()
        client.upload_file(str(file), '', '', ExtraArgs={'Metadata': {'sha256': expected['sha256']}})
        fetch = self.fetch_module()
        with patch.object(fetch, 's3_client', return_value=client):
            new = fetch.download('isolated/test.dump', 'recovered.dump', expected['sha256'])
            self.assertEqual(new.read_bytes(), file.read_bytes())
            with self.assertRaisesRegex(w.Failure, 'download_target_exists'):
                fetch.download('isolated/test.dump', 'recovered.dump')
            self.assertEqual(new.read_bytes(), file.read_bytes())

    def test_corrupt_download_leaves_no_completed_archive(self):
        file, expected = self.archive()
        client = FakeS3()
        client.upload_file(str(file), '', '', ExtraArgs={'Metadata': {'sha256': expected['sha256']}})
        client.get_object = lambda **kwargs: {'Body': io.BytesIO(b'corrupt')}
        fetch = self.fetch_module()
        with patch.object(fetch, 's3_client', return_value=client):
            with self.assertRaisesRegex(w.Failure, 'download_verification_failed'):
                fetch.download('isolated/test.dump', 'recovered.dump')
        self.assertFalse((w.ROOT / 'recovered.dump').exists())
        self.assertFalse(list(w.ROOT.glob('*.partial')))

    def test_download_cannot_escape_backup_directory_or_prefix(self):
        fetch = self.fetch_module()
        with self.assertRaisesRegex(w.Failure, 'invalid_download_target'):
            fetch.download('isolated/test.dump', '../escape.dump')
        with self.assertRaisesRegex(w.Failure, 'invalid_download_key'):
            fetch.download('elsewhere/test.dump', 'recovered.dump')

    def test_typo_cannot_silently_disable_offsite_backup(self):
        os.environ['BACKUP_S3_ENABLED'] = 'treu'
        with self.assertRaisesRegex(w.Failure, 'invalid_configuration'):
            w.cycle()

    def test_failed_old_object_does_not_starve_newer_backups(self):
        self.archive()
        w.atomic('grit-z.dump.pending.json', {'sha256': 'test'})
        with patch.object(w, 'upload', side_effect=[w.Failure('local_archive_changed'), None]) as upload:
            with self.assertRaisesRegex(w.Failure, 'local_archive_changed'):
                w.cycle()
            self.assertEqual(upload.call_count, 2)

    def test_private_backup_template_never_overwrites(self):
        spec = importlib.util.spec_from_file_location('init_backup_env', ROOT / 'deploy/init_backup_env.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = w.ROOT / '.env.backup'
        module.generate(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        with self.assertRaises(FileExistsError):
            module.generate(path)


if __name__ == '__main__':
    unittest.main()
