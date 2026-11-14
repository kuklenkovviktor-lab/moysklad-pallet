"""Synthetic manager-only MoySklad connectivity checks, no live requests."""
import os
import unittest
from unittest.mock import patch
from flask import Flask
from integrations import integrations_bp, _moysklad_health


class IntegrationConnectivityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.secret_key = "only-synthetic"
        cls.app.testing = True
        cls.app.register_blueprint(integrations_bp)

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True

    def test_not_configured_does_not_call_http(self):
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": ""}), \
             patch("integrations.requests.get") as get:
            result = self.client.post("/api/manager/integrations/moysklad/check")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["state"], "unconfigured")
        get.assert_not_called()

    def test_success_read_only_and_no_secret_in_response(self):
        class Response:
            status_code = 200
            def json(self):
                return {"rows": []}
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "not-a-real-token"}), \
             patch("integrations.requests.get", return_value=Response()) as get:
            result = self.client.post("/api/manager/integrations/moysklad/check")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json["state"], "connected")
        self.assertNotIn("not-a-real-token", result.get_data(as_text=True))
        self.assertEqual(get.call_args.kwargs["headers"]["Accept-Encoding"], "gzip")
        self.assertEqual(get.call_args.kwargs["params"], {"limit": 1})
        self.assertEqual(get.call_args.kwargs["timeout"], 8)
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertEqual(get.call_args.args[0],
                         "https://api.moysklad.ru/api/remap/1.2/entity/product")

    def test_invalid_credentials_and_rate_limit(self):
        class Response:
            def __init__(self, code):
                self.status_code = code
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}):
            for code, expected in [(401, "unauthorized"), (403, "unauthorized"),
                                   (429, "unavailable"), (500, "unavailable")]:
                with self.subTest(code=code), \
                     patch("integrations.requests.get", return_value=Response(code)):
                    self.assertEqual(_moysklad_health()["state"], expected)

    def test_nonstandard_response_has_safe_http_status(self):
        class Response:
            status_code = 415
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("integrations.requests.get", return_value=Response()):
            response = self.client.post("/api/manager/integrations/moysklad/check")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["state"], "unavailable")
        self.assertIn("HTTP 415", response.json["details"])
        self.assertNotIn("synthetic", response.get_data(as_text=True))

    def test_timeout_is_unavailable_and_never_exposes_exception(self):
        import requests
        with patch.dict(os.environ, {"MOYSKLAD_TOKEN": "synthetic"}), \
             patch("integrations.requests.get", side_effect=requests.Timeout("SECRET")):
            result = self.client.post("/api/manager/integrations/moysklad/check")
        self.assertEqual(result.json["state"], "unavailable")
        self.assertNotIn("SECRET", result.get_data(as_text=True))

    def test_manager_only_and_no_network_without_auth(self):
        with self.client.session_transaction() as sess:
            sess.clear()
        with patch("integrations.requests.get") as get:
            result = self.client.post("/api/manager/integrations/moysklad/check")
        self.assertEqual(result.status_code, 403)
        get.assert_not_called()

    def test_page_uses_dark_design_and_explicit_check(self):
        html = self.client.get("/manager/integrations").get_data(as_text=True)
        self.assertIn("#09121e", html)
        self.assertIn("Проверить соединение", html)
        self.assertIn("checkMoysklad", html)
        self.assertIn('method:\'POST\'', html)


if __name__ == "__main__":
    unittest.main()
