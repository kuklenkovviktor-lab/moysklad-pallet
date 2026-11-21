"""WB FBS v1.1 HTTP-contract simulations. Only synthetic SQLite and mocked responses.

Every HTTP request is intercepted. No requests to WB, the internet or live DB.
Test order is intentionally: product barcode -> WB sticker barcode.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
SKU = "4601234567890"
STICKER = "SYNTHETIC-WB-STICKER-A"
ACCOUNT = "synthetic-seller"


def order(order_id=84500123, sku=SKU):
    return {
        "id": order_id,
        "article": "SYNTHETIC-CRYSTAL",
        "nmId": 192837465,
        "chrtId": 987654321,
        "warehouseId": 7,
        "skus": [sku],
        "createdAt": "2026-11-21T12:00:00Z",
    }


def sticker(value=STICKER, order_id=84500123):
    return {"orderId": order_id, "barcode": value, "partA": "123",
            "partB": "456", "file": "synthetic-image-not-used"}


class FakeResponse:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self.payload = payload if payload is not None else {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("Mock HTTP " + str(self.status_code))

    def json(self):
        return self.payload


class ScriptedHTTP:
    """Strict sequence of method/endpoint -> response/exception, no real transport."""
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.steps:
            raise AssertionError("Unexpected HTTP call: " + url)
        expected_method, expected_path, outcome = self.steps.pop(0)
        assert method == expected_method, (method, expected_method)
        assert url.endswith(expected_path), (url, expected_path)
        assert "Authorization" in kwargs["headers"]
        assert kwargs["timeout"] == 30
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def assert_consumed(self, case):
        case.assertEqual(self.steps, [], "Not all simulated WB API calls were made")


def response(status=200, payload=None, headers=None):
    return FakeResponse(status, payload, headers)


class WBApiMockTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "DATABASE_URL": "",
            "WMS_REQUIRE_POSTGRES": "0",
            "WMS_DB_PATH": str(Path(tmp.name) / "wb-mocked.sqlite"),
            "WMS_BADGE_SECRET": "synthetic-badge",
            "WMS_TERMINAL_SECRET": "synthetic-terminal",
            "WB_API_TOKEN": "",
            "WB_CONTENT_TOKEN": "",
            "WB_FBS_V11_ENABLED": "0",
        })
        env.start()
        self.addCleanup(env.stop)
        wspec = importlib.util.spec_from_file_location(
            "wms_wb_http_fixture_" + uuid.uuid4().hex, ROOT / "wms.py")
        self.wms = importlib.util.module_from_spec(wspec)
        wspec.loader.exec_module(self.wms)
        modules = patch.dict(sys.modules, {"wms": self.wms})
        modules.start()
        self.addCleanup(modules.stop)
        spec = importlib.util.spec_from_file_location(
            "wb_mock_http_fixture_" + uuid.uuid4().hex, ROOT / "wb_fbs_sync_v11.py")
        self.sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.sync)
        self.sync.init_schema()

    def client(self, steps):
        http = ScriptedHTTP(steps)
        return self.sync.WBClient("synthetic-token", "synthetic-content-token", session=http), http

    def seed(self, status="confirm"):
        self.sync.ingest_orders(ACCOUNT, [order()], {"84500123": status})
        self.sync.ingest_stickers(ACCOUNT, [sticker()])

    def verdict(self, code=STICKER, product=SKU):
        return self.sync.verify_pair(ACCOUNT, product, code)["result"]

    def test_cancellation_from_mock_status_blocks_existing_sticker(self):
        self.seed()
        client, http = self.client([
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "cancel"}]})),
        ])
        self.assertEqual(self.sync.sync_status_once(ACCOUNT, client), 1)
        self.assertEqual(self.verdict(), "BLOCKED_STATUS")
        self.assertEqual(self.wms.query_one(
            "SELECT barcode FROM wb_fbs_stickers WHERE account_id=?",
            (ACCOUNT,))[0], STICKER)
        http.assert_consumed(self)

    def test_status_confirm_to_new_to_complete(self):
        self.seed()
        for status, expected in [("new", "BLOCKED_STATUS"), ("complete", "MATCH")]:
            client, http = self.client([
                ("POST", "/api/v3/orders/status", response(
                    payload={"orders": [{"id": 84500123, "supplierStatus": status}]})),
            ])
            self.sync.sync_status_once(ACCOUNT, client)
            self.assertEqual(self.verdict(), expected)
            http.assert_consumed(self)

    def test_status_unknown_fails_closed(self):
        self.seed()
        client, _ = self.client([
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "unexpected"}]})),
        ])
        self.sync.sync_status_once(ACCOUNT, client)
        self.assertEqual(self.verdict(), "BLOCKED_STATUS")

    def test_full_sync_replaces_sticker_without_duplicate(self):
        self.seed()
        replacement = "SYNTHETIC-WB-STICKER-B"
        client, http = self.client([
            ("GET", "/api/v3/orders", response(payload={"orders": [order()], "next": 0})),
            ("GET", "/api/v3/orders/new", response(payload={"orders": []})),
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "confirm"}]})),
            ("POST", "/api/v3/orders/stickers", response(payload={"stickers": [sticker(replacement)]})),
        ])
        result = self.sync.sync_once(ACCOUNT, client, date_from=1795000000)
        self.assertEqual(result["received"], 1)
        self.assertEqual(self.verdict(STICKER), "UNKNOWN_STICKER")
        self.assertEqual(self.verdict(replacement), "MATCH")
        self.assertEqual(self.wms.query_one(
            "SELECT COUNT(*) FROM wb_fbs_stickers WHERE account_id=?",
            (ACCOUNT,))[0], 1)
        self.assertEqual(self.wms.query_one(
            "SELECT status FROM wb_fbs_sync_runs ORDER BY started_at DESC LIMIT 1")[0], "ok")
        http.assert_consumed(self)

    def test_sticker_api_failure_does_not_erase_cached_sticker(self):
        self.seed()
        client, http = self.client([
            ("GET", "/api/v3/orders", response(payload={"orders": [order()], "next": 0})),
            ("GET", "/api/v3/orders/new", response(payload={"orders": []})),
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "confirm"}]})),
            *[("POST", "/api/v3/orders/stickers", response(503)) for _ in range(4)],
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            with self.assertRaises(requests.HTTPError):
                self.sync.sync_once(ACCOUNT, client)
        self.assertEqual(self.verdict(), "MATCH")
        self.assertEqual(self.wms.query_one(
            "SELECT status,error_count,error_message FROM wb_fbs_sync_runs"
        ), ("error", 1, "HTTPError"))
        http.assert_consumed(self)

    def test_failed_status_request_keeps_previous_cache_and_marks_error(self):
        self.seed()
        client, http = self.client([
            ("GET", "/api/v3/orders", response(payload={"orders": [order()], "next": 0})),
            ("GET", "/api/v3/orders/new", response(payload={"orders": []})),
            *[("POST", "/api/v3/orders/status", response(502)) for _ in range(4)],
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            with self.assertRaises(requests.HTTPError):
                self.sync.sync_once(ACCOUNT, client)
        self.assertEqual(self.verdict(), "MATCH")
        self.assertEqual(self.wms.query_one(
            "SELECT status,error_message FROM wb_fbs_sync_runs"), ("error", "HTTPError"))
        http.assert_consumed(self)

    def test_401_and_403_never_retry(self):
        for code in (401, 403):
            with self.subTest(code=code):
                client, http = self.client([
                    ("GET", "/api/v3/orders/new", response(code)),
                ])
                with self.assertRaises(requests.HTTPError):
                    client.new_orders()
                self.assertEqual(len(http.calls), 1)
                http.assert_consumed(self)

    def test_429_retries_with_retry_after_then_recovers(self):
        client, http = self.client([
            ("GET", "/api/v3/orders/new", response(429, headers={"Retry-After": "2"})),
            ("GET", "/api/v3/orders/new", response(payload={"orders": [order()]})),
        ])
        with patch.object(self.sync.time, "sleep", return_value=None) as delay:
            self.assertEqual(len(client.new_orders()), 1)
        self.assertEqual(len(http.calls), 2)
        self.assertGreaterEqual(delay.call_args.args[0], 2.0)
        http.assert_consumed(self)

    def test_429_exhaustion_raises_after_four_attempts(self):
        client, http = self.client(
            [("GET", "/api/v3/orders/new", response(429)) for _ in range(4)])
        with patch.object(self.sync.time, "sleep", return_value=None):
            with self.assertRaises(requests.HTTPError):
                client.new_orders()
        self.assertEqual(len(http.calls), 4)
        http.assert_consumed(self)

    def test_5xx_retries_and_recovers(self):
        client, http = self.client([
            ("GET", "/api/v3/orders/new", response(500)),
            ("GET", "/api/v3/orders/new", response(503)),
            ("GET", "/api/v3/orders/new", response(payload={"orders": []})),
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            self.assertEqual(client.new_orders(), [])
        self.assertEqual(len(http.calls), 3)
        http.assert_consumed(self)

    def test_network_timeout_retries_and_recovers(self):
        client, http = self.client([
            ("GET", "/api/v3/orders/new", requests.Timeout("mock timeout")),
            ("GET", "/api/v3/orders/new", response(payload={"orders": [order()]})),
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            self.assertEqual(len(client.new_orders()), 1)
        self.assertEqual(len(http.calls), 2)
        http.assert_consumed(self)

    def test_connection_error_exhausts_without_touching_orders(self):
        self.seed()
        client, http = self.client([
            ("GET", "/api/v3/orders", requests.ConnectionError("mock offline"))
            for _ in range(4)
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            with self.assertRaises(requests.ConnectionError):
                self.sync.sync_once(ACCOUNT, client)
        self.assertEqual(self.verdict(), "MATCH")
        self.assertEqual(self.wms.query_one(
            "SELECT status,error_message FROM wb_fbs_sync_runs"), ("error", "ConnectionError"))
        http.assert_consumed(self)

    def test_status_persisted_even_when_sticker_endpoint_fails(self):
        """Status sync must not roll back the WB status on a sticker HTTP outage."""
        self.sync.ingest_orders(ACCOUNT, [order()], {"84500123": "new"})
        client, http = self.client([
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "confirm"}]})),
            *[("POST", "/api/v3/orders/stickers", response(503)) for _ in range(4)],
        ])
        with patch.object(self.sync.time, "sleep", return_value=None):
            with self.assertRaises(requests.HTTPError):
                self.sync.sync_status_once(ACCOUNT, client)
        self.assertEqual(self.wms.query_one(
            "SELECT wb_status FROM wb_fbs_orders"), ("confirm",))
        self.assertEqual(self.verdict(), "UNKNOWN_STICKER")
        http.assert_consumed(self)

    def test_new_order_requires_sticker_before_verification(self):
        client, http = self.client([
            ("GET", "/api/v3/orders/new", response(payload={"orders": [order()]})),
            ("POST", "/api/v3/orders/status", response(
                payload={"orders": [{"id": 84500123, "supplierStatus": "new"}]})),
        ])
        self.assertEqual(self.sync.sync_new_once(ACCOUNT, client)["received"], 1)
        self.assertEqual(self.verdict(), "UNKNOWN_STICKER")
        http.assert_consumed(self)


if __name__ == "__main__":
    unittest.main()
