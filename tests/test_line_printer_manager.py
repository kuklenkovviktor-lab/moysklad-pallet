"""Offline Flask tests of gated manager printer registry on synthetic SQLite."""
import importlib
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask, session


class PrinterManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "print.db")
        module = types.ModuleType("wms")
        def db_connect():
            return sqlite3.connect(path)
        def sql(statement):
            return statement
        def manager_access():
            return session.get("manager_web") is True
        module.db_connect = db_connect
        module.sql = sql
        module.manager_access = manager_access
        module.now_iso = lambda: "2026-11-25T12:00:00+00:00"

        env = patch.dict(os.environ, {"GRIT_LINE_PRINTER_ROUTING_ENABLED": "1"})
        env.start()
        self.addCleanup(env.stop)
        modules = patch.dict(sys.modules, {"wms": module})
        modules.start()
        self.addCleanup(modules.stop)
        # Import via isolated name so no tests reuse a prior module's WMS wiring.
        spec = importlib.util.spec_from_file_location(
            "grit_printer_manager_test_fixture", Path(__file__).resolve().parents[1] / "line_printer_manager.py")
        manager = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(manager)
        self.manager = manager
        app = Flask(__name__)
        app.secret_key = "synthetic-printer-manager-test"
        app.register_blueprint(manager.line_printers_bp)
        self.client = app.test_client()
        self.app = app
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True

    def csrf(self):
        return self.client.get("/api/manager/line-printers").json["csrf"]

    def create(self, **overrides):
        data = {"csrf": self.csrf(), "name": "Принтер А-Б", "lines": ["А", "Б"]}
        data.update(overrides)
        return self.client.post("/api/manager/line-printers", json=data)

    def test_manager_creation_and_58x40_preview(self):
        r = self.create()
        self.assertEqual(r.status_code, 201, r.json)
        self.assertIn("size:58mm 40mm", r.json["label_html"])
        self.assertEqual(r.json["printer"]["lines"], ["А", "Б"])
        lst = self.client.get("/api/manager/line-printers")
        self.assertEqual(len(lst.json["printers"]), 1)
        self.assertNotIn("token_digest", str(lst.json))
        self.assertNotIn("label_html", str(lst.json))
        self.assertIn("background:#0c1423", self.client.get("/manager/line-printers").text)

    def test_requires_manager_and_csrf(self):
        with self.app.test_client() as stranger:
            self.assertEqual(stranger.get("/api/manager/line-printers").status_code, 403)
            self.assertEqual(stranger.post("/api/manager/line-printers", json={
                "name": "bad", "lines": ["А"]}).status_code, 403)
        self.assertEqual(self.client.post("/api/manager/line-printers", json={
            "name": "bad", "lines": ["А"]}).status_code, 403)

    def test_disabled_feature_does_not_create_tables(self):
        with patch.dict(os.environ, {"GRIT_LINE_PRINTER_ROUTING_ENABLED": "0"}):
            self.assertEqual(self.client.get("/api/manager/line-printers").status_code, 404)
            self.assertEqual(self.client.get("/manager/line-printers").status_code, 404)

    def test_toggle_and_qr_rotation(self):
        p = self.create().json["printer"]["printer_id"]
        csrf = self.csrf()
        r = self.client.post(f"/api/manager/line-printers/{p}/update",
                             json={"csrf": csrf, "enabled": False})
        self.assertEqual(r.status_code, 200, r.json)
        r = self.client.post(f"/api/manager/line-printers/{p}/rotate",
                             json={"csrf": csrf})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.client.post(f"/api/manager/line-printers/{p}/update",
                                         json={"csrf": csrf, "enabled": True}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/manager/line-printers/{p}/rotate",
                                         json={"csrf": csrf}).status_code, 200)

    def test_rejects_invalid_lines(self):
        self.assertEqual(self.create(lines=["A-05"]).status_code, 400)
        self.assertEqual(self.create(lines=[]).status_code, 400)


if __name__ == "__main__":
    unittest.main()
