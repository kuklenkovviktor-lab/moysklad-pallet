"""Frozen main HTML contract plus real Flask dispatch, with no DB/network access.

Fingerprints were captured before extraction from main
3935c9ef1753f1ea599f2ed2626e0e68bfa20699. Do not regenerate to accept UI changes.
"""
import ast
from contextlib import ExitStack
import hashlib
import importlib
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RAW_HTML = "6d538ab870e0e25b5d187cdc74ef3a312c1d0fb24b3ebe851cf7f191e723484a"
THEMED_HTML = "c7c5da60eaa0d7d9dceb1284b788a90d8a53e31ff27d977b589f23e5a5cd50f7"


class ReceivingMonitorExtractionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {
            "SECRET_KEY": "synthetic-monitor", "WMS_BADGE_SECRET": "synthetic-monitor",
            "WMS_TERMINAL_SECRET": "synthetic-monitor", "DATABASE_URL": "",
            "WMS_REQUIRE_POSTGRES": "0", "WMS_DB_PATH": str(Path(directory) / "unused.sqlite"),
            "MOYSKLAD_TOKEN": "", "WB_API_TOKEN": "", "WB_CONTENT_TOKEN": "",
            "WMS_MIGRATE_SQLITE_FROM_S3": "0",
        }))
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        self.stack.enter_context(patch.object(threading.Thread, "start", side_effect=AssertionError("Background tasks forbidden")))
        self.module = importlib.import_module("app")
        self.wms = importlib.import_module("wms")
        self.stack.enter_context(patch.object(self.module, "_start_wms_initializer"))
        self.stack.enter_context(patch.object(self.module._WMS_READY, "is_set", return_value=True))
        self.stack.enter_context(patch.object(self.wms, "ensure_db"))
        self.stack.enter_context(patch.object(self.wms, "db_connect", side_effect=AssertionError("DB forbidden")))
        self.query = self.stack.enter_context(patch.object(self.wms, "query_all", return_value=[]))
        self.totals = self.stack.enter_context(patch.object(self.wms, "query_one", return_value=(0,0,0,0)))
        self.client = self.module.app.test_client()

    def login(self, **values):
        with self.client.session_transaction(base_url="https://localhost") as session:
            session.update(values)

    def get(self, path):
        return self.client.get(path, base_url="https://localhost")

    def test_original_html_bytes_and_legacy_import_are_identical(self):
        from frontend.pages.receiving_monitor.ui import RECEIVING_MONITOR_PAGE
        self.assertIs(self.wms.RECEIVING_MONITOR_PAGE, RECEIVING_MONITOR_PAGE)
        self.assertEqual(hashlib.sha256(RECEIVING_MONITOR_PAGE.encode("utf-8")).hexdigest(), RAW_HTML)

    def test_manager_response_matches_pre_extraction_bytes_with_real_middleware(self):
        self.login(manager_web=True)
        response = self.get("/receiving-monitor")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/html")
        self.assertEqual(hashlib.sha256(response.data).hexdigest(), THEMED_HTML)
        self.assertEqual(response.data.count(b'/grit-ui.css?v=3'), 1)
        self.assertNotIn(b"verifyShift", response.data)
        self.query.assert_not_called()

    def test_anonymous_employee_and_partner_sessions_stay_denied(self):
        for session in ({}, {"employee_id": "synthetic", "attendance_verified": True},
                        {"partner_id": "synthetic"}):
            with self.subTest(session=session):
                self.client = self.module.app.test_client()
                self.login(**session)
                response = self.get("/receiving-monitor")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/manager-login")
                api = self.get("/api/receiving/overview?q=SYNTHETIC")
                self.assertEqual(api.status_code, 403)
                self.assertEqual(api.json, {"error": "Требуется вход в ПК-панель"})
        self.query.assert_not_called()

    def test_theme_remains_public_and_dark(self):
        response = self.get("/grit-ui.css")
        from grit_theme import GRIT_CSS
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "text/css")
        self.assertEqual(response.data, GRIT_CSS.encode("utf-8"))
        self.assertIn(b"--g-bg:#09121e", response.data)
        self.assertIn(b"@media print", response.data)

    def test_route_and_api_implementations_match_main_including_decorators(self):
        expected = {
            "receiving_monitor_page": "eefc26666a6c910e827625e80cf4e3725b12f223f0919bae09369474c3aa79c1",
            "receiving_overview_api": "6f2416a48b37f4c0cd935ba82ab3cf5b4cdacb1cd6429dc1a54fea53421239f5",
        }
        tree = ast.parse((ROOT / "wms.py").read_text(encoding="utf-8"))
        for name, fingerprint in expected.items():
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertEqual(hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(), fingerprint)

    def test_api_preserves_period_payload_and_get_only(self):
        self.login(manager_web=True)
        self.totals.return_value = (12.5, 2, 1, 1)
        self.query.return_value = [('2026-11-26T01:00:00Z', 'Тест', 'SYNTH', 'Товар',
                                    'SYNTH-CODE', 12.5, 'doc-id', 'Документ', 'scan')]
        period = ('2026-11-26T00:00:00Z', '2026-11-27T00:00:00Z')
        response = self.get('/api/receiving/overview?from='+period[0]+'&to='+period[1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, dict(units=12.5, operations=2, workers=1, completed=1,
            recent=[dict(created_at='2026-11-26T01:00:00Z', employee='Тест', article='SYNTH',
                         product_name='Товар', object_code='SYNTH-CODE', quantity=12.5,
                         document_id='doc-id', document_name='Документ', event_type='scan')]))
        self.assertEqual(self.totals.call_args.args[1], period)
        self.assertEqual(self.query.call_args.args[1], period)
        for path in ('/receiving-monitor', '/api/receiving/overview'):
            self.assertEqual(self.client.post(path, base_url='https://localhost').status_code, 405)

    def test_empty_api_preserves_default_utc_period(self):
        from datetime import datetime, timezone
        self.login(manager_web=True)
        with patch.object(self.wms, 'datetime') as clock:
            clock.now.return_value = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
            response = self.get('/api/receiving/overview')
        self.assertEqual(response.json, dict(units=0.0, operations=0, workers=0, completed=0, recent=[]))
        self.assertEqual(self.query.call_args.args[1],
                         ('2026-11-26T00:00:00+00:00', '2026-11-26T23:59:59.999999+00:00'))
