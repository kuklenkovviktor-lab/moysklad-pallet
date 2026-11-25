"""ONIX 1.0 auth: only printed signed employee badges, independent PC manager login."""
import os
import tempfile
import unittest
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("SECRET_KEY", "onix-auth-only-test-secret")
os.environ.setdefault("WMS_BADGE_SECRET", "onix-auth-test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "onix-auth-test-terminal-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_tmp.name, "auth-tests.db")

import app
import wms


class OnixAuthBadgeSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        wms.init_wms_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        startup = patch.object(app, "_start_wms_initializer")
        startup.start()
        self.addCleanup(startup.stop)

    def login(self, code):
        with patch.object(wms, "ensure_active_shift", return_value=("test-shift", False)), \
             patch.object(wms, "log_event"):
            return self.client.post("/api/auth/login", json={"badge_code": code})

    def test_hmac_badge_printed_in_manager_panel_still_logs_in(self):
        result = self.login(wms.make_badge_code("denis"))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["employee"]["name"], "Денис")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"], "denis")
            self.assertTrue(sess.permanent)
        me = self.client.get("/api/auth/me")
        self.assertEqual(me.status_code, 200, me.json)
        self.assertEqual(me.json["employee"]["id"], "denis")

    def test_signed_numeric_scanner_badge_still_logs_in(self):
        result = self.login(wms.make_badge_scanner_code("anton"))
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(result.json["employee"]["id"], "anton")

    def test_guessable_numbers_and_ids_can_no_longer_impersonate(self):
        for code in ("1", "5", "ONIX-BADGE-1", "ONIX-BADGE-5",
                     "ONIX-EMP-viktor", "ONIX-EMP-denis"):
            with self.subTest(code=code):
                response = self.login(code)
                self.assertEqual(response.status_code, 404, (code, response.json))
                with self.client.session_transaction() as sess:
                    self.assertIsNone(sess.get("employee_id"))

    def test_occupied_terminal_rejects_employee_switch_without_supervisor(self):
        first = self.login(wms.make_badge_code("denis"))
        self.assertEqual(first.status_code, 200, first.json)
        second = self.login(wms.make_badge_code("anton"))
        self.assertEqual(second.status_code, 409, second.json)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"], "denis")

    def test_pc_manager_legacy_link_does_not_require_badge(self):
        with patch.object(wms, "MANAGER_WEB_PASSWORD", "local-tests-only-password"):
            logged = self.client.post("/api/manager/login",
                                      json={"password": "local-tests-only-password"})
        self.assertEqual(logged.status_code, 200, logged.json)
        redirect = self.client.get("/fbs-manager")
        self.assertEqual(redirect.status_code, 302)
        self.assertTrue(redirect.location.endswith("/stats"), redirect.location)
        stats = self.client.get("/stats")
        self.assertEqual(stats.status_code, 302)
        self.assertTrue(stats.location.endswith("/manager"))
        manager = self.client.get("/manager")
        self.assertEqual(manager.status_code, 200)

    def test_staff_or_anonymous_does_not_enter_pc_manager(self):
        anonymous = self.client.get("/fbs-manager")
        self.assertEqual(anonymous.status_code, 302)
        self.assertTrue(anonymous.location.endswith("/manager-login"),
                        anonymous.location)
        with self.client.session_transaction() as sess:
            sess["employee_id"] = "denis"
        staff = self.client.get("/fbs-manager")
        self.assertEqual(staff.status_code, 302)
        self.assertTrue(staff.location.endswith("/manager-login"), staff.location)
        forbidden = self.client.get("/api/manager/warehouse-map")
        self.assertEqual(forbidden.status_code, 403)


if __name__ == "__main__":
    unittest.main()
