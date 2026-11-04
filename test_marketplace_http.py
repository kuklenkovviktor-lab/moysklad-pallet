"""GRIT marketplace HTTP client: deterministic transport and security regression tests."""
import os
import unittest
from unittest.mock import patch

from marketplace_http import (
    Credentials, JOURNAL, LocalRateLimiter, MarketplaceHTTPError,
    MockTransport, SyncJournal, _perform, auth_headers,
    authorized_read_once, mock_sync, request_spec,
)


class MarketHTTPTests(unittest.TestCase):
    def setUp(self):
        self.journal = SyncJournal(capacity=50)
        self.creds = Credentials(token="SECRET-TEST", client_id="CLIENT-TEST", business_id=456)
        self.no_wait = LocalRateLimiter(clock=lambda: 0.0, sleep=lambda _: None)

    def call(self, platform, scenario="ok"):
        transport = MockTransport(platform, scenario)
        try:
            result = _perform(platform, "SELLER-TEST", self.creds, transport,
                              self.no_wait, lambda _: None, self.journal, "mock")
            return result, transport
        except MarketplaceHTTPError:
            return None, transport

    def test_real_pinned_https_hosts_and_read_only_methods(self):
        expected = {
            "wildberries": ("GET", "https://marketplace-api.wildberries.ru/api/v3/orders/new"),
            "ozon": ("POST", "https://api-seller.ozon.ru/v4/posting/fbs/list"),
            "yandex": ("POST", "https://api.partner.market.yandex.ru/v1/businesses/456/orders"),
        }
        for platform, pair in expected.items():
            method, url, payload = request_spec(platform, self.creds)
            self.assertEqual((method, url), pair)
            self.assertTrue(url.startswith("https://"))
            result, transport = self.call(platform)
            self.assertIsNotNone(result)
            self.assertTrue(result["orders"])
            self.assertEqual(transport.calls[0]["allow_redirects"], False)
            self.assertEqual(transport.calls[0]["timeout"], (4, 12))

    def test_headers_are_platform_scoped(self):
        wb = auth_headers("wildberries", self.creds)
        ozon = auth_headers("ozon", self.creds)
        yandex = auth_headers("yandex", self.creds)
        self.assertEqual(wb["Authorization"], "SECRET-TEST")
        self.assertNotIn("Api-Key", wb)
        self.assertEqual(ozon["Client-Id"], "CLIENT-TEST")
        self.assertEqual(ozon["Api-Key"], "SECRET-TEST")
        self.assertNotIn("Authorization", ozon)
        self.assertEqual(yandex["Api-Key"], "SECRET-TEST")
        self.assertNotIn("Client-Id", yandex)

    def test_normalized_results_and_no_network_mock(self):
        for platform, expected in (("wildberries", 2), ("ozon", 1), ("yandex", 1)):
            result = mock_sync(platform, journal=self.journal)
            self.assertEqual(result["count"], expected)
            self.assertTrue(result["no_real_network"])
            self.assertEqual(result["http_calls"], 1)
        self.assertEqual(len(self.journal.recent()), 3)

    def test_429_retry_then_success(self):
        result, transport = self.call("ozon", "rate_limit")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.journal.recent(1)[0]["status"], "ok")

    def test_long_retry_after_is_not_slept_through(self):
        result, transport = self.call("wildberries", "rate_limit_long")
        self.assertIsNone(result)
        self.assertEqual(len(transport.calls), 1)
        entry = self.journal.recent(1)[0]
        self.assertEqual(entry["error_code"], "rate_limited")
        self.assertEqual(entry["http_status"], 429)

    def test_401_is_not_retried_and_errors_are_sanitized(self):
        result, transport = self.call("yandex", "unauthorized")
        self.assertIsNone(result)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(self.journal.recent(1)[0]["error_code"], "authorization_denied")
        self.assertNotIn("SECRET-TEST", str(self.journal.recent()))

    def test_503_retry_exhausted(self):
        result, transport = self.call("ozon", "server_error")
        self.assertIsNone(result)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.journal.recent(1)[0]["error_code"], "remote_unavailable")

    def test_invalid_json_and_schema_rejected(self):
        result, transport = self.call("ozon", "invalid_json")
        self.assertIsNone(result)
        self.assertEqual(self.journal.recent(1)[0]["error_code"], "invalid_json")

    def test_live_call_disabled_even_with_transport(self):
        with patch.dict(os.environ, {"GRIT_MARKETPLACE_LIVE_READ_ENABLED": "0"}, clear=False):
            with self.assertRaises(MarketplaceHTTPError) as exc:
                authorized_read_once("wildberries", "SELLER-TEST",
                                     transport=MockTransport("wildberries"),
                                     journal=self.journal)
            self.assertEqual(exc.exception.category, "live_read_disabled")
        self.assertEqual(self.journal.recent(), [])

    def test_invalid_provider_never_reaches_transport(self):
        with self.assertRaises(MarketplaceHTTPError):
            mock_sync("external-host", journal=self.journal)

    def test_rate_limiter_serializes_local_calls(self):
        counter = [0.0]
        waits = []
        def clock():
            return counter[0]
        def sleep(s):
            waits.append(s)
            counter[0] += s
        limiter = LocalRateLimiter(clock=clock, sleep=sleep)
        limiter.acquire("wildberries")
        limiter.acquire("wildberries")
        self.assertEqual(waits, [1.0])

    def test_manager_only_csrf_and_dedicated_audit_table(self):
        from pathlib import Path
        routes = Path("integrations.py").read_text(encoding="utf-8")
        auth = Path("app.py").read_text(encoding="utf-8")
        journal = Path("marketplace_http_journal.py").read_text(encoding="utf-8")
        self.assertIn("hmac.compare_digest", routes)
        self.assertIn("X-GRIT-CSRF", routes)
        self.assertIn("PersistentJournal().recent(limit=40)", routes)
        self.assertIn("grit_marketplace_sync_log", journal)
        self.assertNotIn("INSERT INTO scans", journal)
        self.assertNotIn("INSERT INTO shifts", journal)
        allowed = auth.split("manager_paths = {",1)[1].split("}",1)[0]
        for endpoint in ("/manager/integrations/http", "/api/manager/integrations/http/mock",
                         "/api/manager/integrations/http/journal"):
            self.assertIn(endpoint, allowed)


if __name__ == "__main__":
    unittest.main()
