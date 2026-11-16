"""Mocked WB Marketplace connection checks, no real tokens or seller data."""
import unittest
from unittest.mock import Mock, patch
from flask import Flask
from integrations import integrations_bp, _wb_api_health


class WildberriesHealthTests(unittest.TestCase):
    def test_unconfigured(self):
        with patch.dict("os.environ", {"WB_API_TOKEN": ""}):
            result = _wb_api_health()
        self.assertEqual(result["state"], "unconfigured")

    def test_token_passed_only_in_header_and_ping_only(self):
        response = Mock(status_code=200)
        response.json.return_value = {"Status": "OK", "TS": "2026-11-16T12:00:00Z"}
        with patch.dict("os.environ", {"WB_API_TOKEN": "SYNTHETIC-TOKEN"}), \
             patch("integrations.requests.get", return_value=response) as get:
            result = _wb_api_health()
        self.assertEqual(result["state"], "connected")
        args, kwargs = get.call_args
        self.assertEqual(args[0], "https://marketplace-api.wildberries.ru/ping")
        self.assertEqual(kwargs["headers"]["Authorization"], "SYNTHETIC-TOKEN")
        self.assertNotIn("SYNTHETIC-TOKEN", repr(result))
        self.assertFalse(kwargs["allow_redirects"])

    def test_invalid_token_and_rate_limit(self):
        for status, expected in ((401, "unauthorized"), (403, "unauthorized"),
                                 (429, "unavailable"), (500, "unavailable")):
            with self.subTest(status=status), \
                 patch.dict("os.environ", {"WB_API_TOKEN": "SYNTHETIC-TOKEN"}), \
                 patch("integrations.requests.get", return_value=Mock(status_code=status)):
                self.assertEqual(_wb_api_health()["state"], expected)

    def test_manager_only(self):
        app = Flask(__name__)
        app.secret_key = "synthetic"
        app.register_blueprint(integrations_bp)
        client = app.test_client()
        self.assertEqual(client.post("/api/manager/integrations/wildberries/check").status_code, 403)
        with client.session_transaction() as session:
            session["manager_web"] = True
        with patch("integrations._wb_api_health", return_value={"state": "unconfigured"}):
            self.assertEqual(client.post("/api/manager/integrations/wildberries/check").status_code, 200)
        ui = client.get("/manager/integrations").get_data(as_text=True)
        self.assertIn("Проверить WB API", ui)


if __name__ == "__main__":
    unittest.main()
