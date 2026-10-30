"""Regression: Android Chrome/PWA must not turn a Render wake-up into ERR_FAILED.

No real database or MoySklad operations are performed.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET", "onix-pwa-test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "onix-pwa-test-terminal")
os.environ.setdefault("SECRET_KEY", "onix-pwa-test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "onix-test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_tmp.name, "pwa-test.db")

import app
import wms


class MobileNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wms.init_wms_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        startup = patch.object(app, "_start_wms_initializer")
        startup.start()
        self.addCleanup(startup.stop)

    def test_service_worker_does_not_intercept_get_or_navigation(self):
        sw = self.client.get("/sw.js")
        self.assertEqual(sw.status_code, 200, sw.data[:300])
        self.assertIn("application/javascript", sw.content_type)
        self.assertIn("no-store", sw.headers.get("Cache-Control", ""))
        self.assertEqual(sw.headers["Service-Worker-Allowed"], "/")
        script = sw.get_data(as_text=True)
        self.assertIn("skipWaiting()", script)
        self.assertIn("clients.claim()", script)
        self.assertNotIn("addEventListener('fetch'", script)
        self.assertNotIn('addEventListener("fetch"', script)
        self.assertNotIn("respondWith(", script)
        self.assertNotIn("caches.match(", script)

    def test_pwa_assets_refresh_without_employee_cookie(self):
        manifest = self.client.get("/manifest.webmanifest")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("manifest+json", manifest.content_type)
        self.assertIn("no-store", manifest.headers.get("Cache-Control", ""))

    def test_replenishment_requires_badge_and_loads_with_employee(self):
        anonymous = self.client.get("/replenishment")
        self.assertEqual(anonymous.status_code, 302)
        self.assertTrue(anonymous.location.endswith("/login"), anonymous.location)
        with self.client.session_transaction() as sess:
            sess["employee_id"] = "denis"
        active = self.client.get("/replenishment")
        self.assertEqual(active.status_code, 200, active.data[:250])
        self.assertIn("Пополнение".encode("utf-8"), active.data)


if __name__ == "__main__":
    unittest.main()
