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
RAW_HTML = "3f02cc8e6f1db569778ccaa605ed99a452c8339bfe47ca65f9b97767ff4c04cc"
THEMED_HTML = "f91c2c24f57a72bed2ee0078815c7d3c8a5d0bdf9fdc81e96f0bb7e5e5c1349f"


class HistoryExtractionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {
            "SECRET_KEY": "synthetic-history", "WMS_BADGE_SECRET": "synthetic-history",
            "WMS_TERMINAL_SECRET": "synthetic-history", "DATABASE_URL": "",
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
        self.client = self.module.app.test_client()

    def login(self, **values):
        with self.client.session_transaction(base_url="https://localhost") as session:
            session.update(values)

    def get(self, path):
        return self.client.get(path, base_url="https://localhost")

    def test_original_html_bytes_and_legacy_import_are_identical(self):
        from frontend.pages.barcode_history.ui import HISTORY_PAGE
        self.assertIs(self.wms.HISTORY_PAGE, HISTORY_PAGE)
        self.assertEqual(hashlib.sha256(HISTORY_PAGE.encode("utf-8")).hexdigest(), RAW_HTML)

    def test_manager_response_matches_pre_extraction_bytes_with_real_middleware(self):
        self.login(manager_web=True)
        response = self.get("/history")
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
                response = self.get("/history")
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.headers["Location"], "/manager-login")
                api = self.get("/api/history?q=SYNTHETIC")
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

    def test_api_empty_search_and_read_only_methods(self):
        self.login(manager_web=True)
        self.assertEqual(self.get("/api/history?q=%20").json, {"rows": []})
        self.query.assert_not_called()
        for path in ("/history", "/api/history"):
            self.assertEqual(self.client.post(path, base_url="https://localhost").status_code, 405)

    def test_api_synthetic_payload_query_and_sort_are_preserved(self):
        self.login(manager_web=True)
        self.query.side_effect = [
            [("2026-11-25T07:00:00Z", "Тест", "SYNTH", "Товар", "PRODUCT", "WB", "normal")],
            [("2026-11-25T08:00:00Z", "Тест", "receiving", "scan", "SYNTH", 2, "ok", "SYNTH", "Товар")],
        ]
        response = self.get("/api/history?q=%20SYNTH%20")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"rows": [
            dict(created_at="2026-11-25T08:00:00Z", employee="Тест", operation_type="receiving",
                 event_type="scan", object_code="SYNTH", quantity=2, result="ok", article="SYNTH", product_name="Товар"),
            dict(created_at="2026-11-25T07:00:00Z", employee="Тест", operation_type="fbs",
                 event_type="fbs_scan", object_code="WB", quantity=1, result="normal", article="SYNTH", product_name="Товар"),
        ]})
        self.assertEqual(self.query.call_args_list[0].args[1], ("%SYNTH%",) * 4)
        self.assertEqual(self.query.call_args_list[1].args[1], ("%SYNTH%",) * 5)

    def test_route_and_api_implementations_match_main_including_decorators(self):
        expected = {
            "history_page": "9ad836b0c779abd1a101c1fb00855f9224610fc4d86455b7123100e87598d4d9",
            "history_api": "cb5b43c9ce2512cf9f2876da61c2a14147b50f8ae0de649156d438b7b4e0fa2c",
        }
        tree = ast.parse((ROOT / "wms.py").read_text(encoding="utf-8"))
        for name, fingerprint in expected.items():
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
            self.assertEqual(hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(), fingerprint)
