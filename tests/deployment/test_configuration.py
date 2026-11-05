"""Deployment regression checks; no Docker daemon or production services needed."""
import importlib.util
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy' / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class DeploymentTests(unittest.TestCase):
    def test_secrets_exclusive_private_and_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            module('init_env').generate(path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            before = path.read_bytes()
            with self.assertRaises(FileExistsError):
                module('init_env').generate(path)
            self.assertEqual(path.read_bytes(), before)
            values = dict(line.split('=', 1) for line in before.decode().splitlines() if '=' in line)
            names = ['POSTGRES_PASSWORD', 'SECRET_KEY', 'WMS_BADGE_SECRET', 'WMS_TERMINAL_SECRET', 'MANAGER_WEB_PASSWORD']
            self.assertEqual(len({values[x] for x in names}), 5)
            self.assertTrue(all(len(values[x]) == 64 for x in names))

    def test_entrypoint_forces_local_database_and_quotes_password(self):
        with patch.dict(os.environ, {'DATABASE_URL': 'postgresql://production/never', 'POSTGRES_PASSWORD': 'x:@/ y', 'WMS_REQUIRE_POSTGRES': '0'}, clear=True):
            module('entrypoint').configure()
            self.assertEqual(os.environ['DATABASE_URL'], 'postgresql://grit:x%3A%40%2F%20y@db:5432/grit')
            self.assertEqual(os.environ['WMS_REQUIRE_POSTGRES'], '1')
            self.assertNotIn('POSTGRES_PASSWORD', os.environ)

    def test_failed_startup_cannot_pass_health(self):
        import io
        from contextlib import closing
        health = module('healthcheck')
        with patch.object(health, 'urlopen', return_value=closing(io.BytesIO(b'{"ready":false}'))), patch.object(health.psycopg, 'connect') as connect:
            with self.assertRaises(RuntimeError):
                health.check()
            connect.assert_not_called()

    def test_healthy_http_with_failed_database_fails_health(self):
        import io
        from contextlib import closing
        health = module('healthcheck')
        with patch.object(health, 'urlopen', return_value=closing(io.BytesIO(b'{"ready":true}'))), patch.dict(os.environ, {'POSTGRES_PASSWORD': 'test-only'}), patch.object(health.psycopg, 'connect', side_effect=RuntimeError('DB unavailable')):
            with self.assertRaises(RuntimeError):
                health.check()

    def test_no_secrets_in_docker_context(self):
        ignore = (ROOT / '.dockerignore').read_text().splitlines()
        for pattern in ['.env', '.env.*', '*.pem', '*.key', '*.dump', '.git']:
            self.assertIn(pattern, ignore)


if __name__ == '__main__':
    unittest.main()
