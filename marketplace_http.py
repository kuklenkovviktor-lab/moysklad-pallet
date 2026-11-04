"""Read-only, transport-injectable marketplace HTTP adapters for GRIT.

These adapters never mutate marketplace orders or WMS picking tables.
Production HTTP is explicitly disabled until authorized credentials AND
GRIT_MARKETPLACE_LIVE_READ_ENABLED=1. The manager UI only runs mock transport.

Only fixed HTTPS hosts/paths are used, no redirects, and logs contain metadata
only. Seller onboarding / OAuth / encrypted multi-tenant secret vault are NOT
implemented: environment-only credentials are for one explicitly authorized
seller per deployment.
"""
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import threading
import time
import uuid

import requests

from marketplace_lab import NORMALIZERS, AdapterError, fixtures

BASES = {
    "wildberries": "https://marketplace-api.wildberries.ru",
    "ozon": "https://api-seller.ozon.ru",
    "yandex": "https://api.partner.market.yandex.ru",
}
FIXED_ENDPOINTS = {
    "wildberries": ("GET", "/api/v3/orders/new"),
    "ozon": ("POST", "/v4/posting/fbs/list"),
    "yandex": ("POST", "/v1/businesses/{business_id}/orders"),
}
# Conservative local process limits, NOT the marketplace's contractual quotas.
MIN_INTERVAL = {"wildberries": 1.0, "ozon": 2.0, "yandex": 1.0}
MAX_ATTEMPTS = 3
MAX_WAIT_SECONDS = 8.0
MAX_BODY_BYTES = 3 * 1024 * 1024
MAX_LOG_ENTRIES = 200


class MarketplaceHTTPError(RuntimeError):
    def __init__(self, category, status=None, retry_after=None):
        super().__init__(category)
        self.category = category
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class Credentials:
    token: str
    client_id: str = ""
    business_id: int = 0


def load_authorized_credentials(platform):
    """Explicit, deployment-scoped environment secrets, never browser input."""
    names = {
        "wildberries": ("GRIT_MP_WB_TOKEN",),
        "ozon": ("GRIT_MP_OZON_CLIENT_ID", "GRIT_MP_OZON_API_KEY"),
        "yandex": ("GRIT_MP_YANDEX_API_KEY", "GRIT_MP_YANDEX_BUSINESS_ID"),
    }
    if platform not in names:
        raise MarketplaceHTTPError("unsupported_provider")
    vals = [os.environ.get(name, "").strip() for name in names[platform]]
    if not all(vals):
        raise MarketplaceHTTPError("credentials_not_configured")
    if platform == "ozon":
        return Credentials(token=vals[1], client_id=vals[0])
    if platform == "yandex":
        if not vals[1].isdigit() or int(vals[1]) < 1:
            raise MarketplaceHTTPError("invalid_business_id")
        return Credentials(token=vals[0], business_id=int(vals[1]))
    return Credentials(token=vals[0])


def auth_headers(platform, credentials):
    if not credentials or not credentials.token:
        raise MarketplaceHTTPError("missing_credentials")
    if platform == "wildberries":
        # WB uses its seller's authorized marketplace token in Authorization.
        return {"Authorization": credentials.token, "Accept": "application/json"}
    if platform == "ozon":
        if not credentials.client_id:
            raise MarketplaceHTTPError("missing_client_id")
        return {"Client-Id": credentials.client_id, "Api-Key": credentials.token,
                "Content-Type": "application/json", "Accept": "application/json"}
    if platform == "yandex":
        if not credentials.business_id:
            raise MarketplaceHTTPError("missing_business_id")
        return {"Api-Key": credentials.token, "Content-Type": "application/json",
                "Accept": "application/json"}
    raise MarketplaceHTTPError("unsupported_provider")


def request_spec(platform, credentials):
    """Fixed, read-only list endpoints. Never accepts an arbitrary URL."""
    if platform not in FIXED_ENDPOINTS:
        raise MarketplaceHTTPError("unsupported_provider")
    method, path = FIXED_ENDPOINTS[platform]
    payload = None
    if platform == "ozon":
        # Date window and pagination intentionally bounded to a single sample page.
        # Verify full API schema before enabling real multi-page synchronization.
        payload = {"dir": "ASC", "filter": {}, "limit": 50, "offset": 0,
                   "with": {"analytics_data": False, "financial_data": False}}
    elif platform == "yandex":
        if credentials.business_id < 1:
            raise MarketplaceHTTPError("missing_business_id")
        path = path.format(business_id=credentials.business_id)
        payload = {"programTypes": ["FBS"]}
    return method, BASES[platform] + path, payload


class LocalRateLimiter:
    """Thread-safe process-local throttle. Distributed deployment needs shared quota."""
    def __init__(self, clock=None, sleep=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.next_at = {}

    def acquire(self, platform):
        if platform not in MIN_INTERVAL:
            raise MarketplaceHTTPError("unsupported_provider")
        with self.lock:
            now = self.clock()
            ready_at = max(now, self.next_at.get(platform, now))
            self.next_at[platform] = ready_at + MIN_INTERVAL[platform]
        delay = ready_at - now
        if delay > 0:
            self.sleep(delay)
        return round(max(0, delay), 3)


class SyncJournal:
    """Bounded, process-local journal with no secrets, PII or response payloads."""
    def __init__(self, capacity=MAX_LOG_ENTRIES):
        self.entries = deque(maxlen=capacity)
        self.lock = threading.Lock()

    def record(self, platform, seller, mode, status, attempts, count=0,
               http_status=None, error="", delay=0):
        entry = {
            "id": uuid.uuid4().hex[:12],
            "at": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "seller": seller,
            "mode": mode,
            "status": status,
            "attempts": attempts,
            "orders": count,
            "http_status": http_status,
            "error_code": error,
            "wait_seconds": round(delay, 2),
        }
        with self.lock:
            self.entries.appendleft(entry)
        return entry

    def recent(self, limit=40):
        with self.lock:
            return list(self.entries)[:max(1, min(100, int(limit)))]


JOURNAL = SyncJournal()
THROTTLE = LocalRateLimiter()


def _retry_delay(response, attempt):
    header = response.headers.get("Retry-After", "")
    try:
        value = float(header)
        if value >= 0:
            return value
    except (TypeError, ValueError, OverflowError):
        pass
    return min(MAX_WAIT_SECONDS, float(2 ** (attempt - 1)))


def _perform(platform, seller, credentials, transport, limiter, sleep, journal, mode):
    if not seller or len(seller) > 100:
        raise MarketplaceHTTPError("invalid_seller")
    method, url, payload = request_spec(platform, credentials)
    headers = auth_headers(platform, credentials)
    attempts = 0
    delay_total = 0.0
    last_http = None
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            attempts = attempt
            delay_total += limiter.acquire(platform)
            try:
                response = transport.request(
                    method, url, headers=headers, json=payload, timeout=(4, 12),
                    allow_redirects=False,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                if attempt == MAX_ATTEMPTS:
                    raise MarketplaceHTTPError("network_failure")
                delay = min(MAX_WAIT_SECONDS, float(2 ** (attempt - 1)))
                sleep(delay)
                delay_total += delay
                continue
            last_http = response.status_code
            if 300 <= response.status_code < 400:
                raise MarketplaceHTTPError("redirect_blocked", response.status_code)
            if response.status_code in (429, 420, 500, 502, 503, 504):
                delay = _retry_delay(response, attempt)
                # Do not block worker threads for arbitrarily long Retry-After.
                if delay > MAX_WAIT_SECONDS or attempt == MAX_ATTEMPTS:
                    raise MarketplaceHTTPError("rate_limited" if response.status_code in (429, 420)
                                               else "remote_unavailable", response.status_code,
                                               retry_after=delay)
                sleep(delay)
                delay_total += delay
                continue
            if response.status_code in (401, 403):
                raise MarketplaceHTTPError("authorization_denied", response.status_code)
            if response.status_code >= 400:
                raise MarketplaceHTTPError("remote_rejected", response.status_code)
            if response.status_code != 200:
                raise MarketplaceHTTPError("unexpected_status", response.status_code)
            raw_content = response.content
            if len(raw_content) > MAX_BODY_BYTES:
                raise MarketplaceHTTPError("response_too_large", response.status_code)
            try:
                body = response.json()
            except ValueError:
                raise MarketplaceHTTPError("invalid_json", response.status_code)
            if not isinstance(body, dict):
                raise MarketplaceHTTPError("invalid_body", response.status_code)
            try:
                orders = NORMALIZERS[platform](body, seller)
            except (AdapterError, KeyError, TypeError) as exc:
                raise MarketplaceHTTPError("invalid_schema", response.status_code) from None
            journal.record(platform, seller, mode, "ok", attempts, len(orders),
                           last_http, delay=delay_total)
            return {"platform": platform, "seller_id": seller, "orders": orders,
                    "count": len(orders), "mode": mode, "attempts": attempts}
        raise MarketplaceHTTPError("attempts_exhausted")
    except MarketplaceHTTPError as exc:
        journal.record(platform, seller, mode, "error", attempts, 0,
                       exc.status or last_http, error=exc.category, delay=delay_total)
        raise
    except Exception:
        journal.record(platform, seller, mode, "error", attempts, 0,
                       last_http, error="unexpected_transport", delay=delay_total)
        raise MarketplaceHTTPError("unexpected_transport") from None


class MockResponse:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        import json
        self.content = json.dumps(self._body, ensure_ascii=False).encode("utf-8")

    def json(self):
        return self._body


class MockTransport:
    """Captures actual request options; returns synthetic platform-shaped JSON."""
    def __init__(self, platform, scenario="ok"):
        self.platform = platform
        self.scenario = scenario
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url,
                           "headers": dict(kwargs.get("headers", {})),
                           "timeout": kwargs.get("timeout"),
                           "allow_redirects": kwargs.get("allow_redirects")})
        if self.scenario == "unauthorized":
            return MockResponse(401, {"error": "synthetic unauthorized"})
        if self.scenario == "rate_limit" and len(self.calls) <= 2:
            return MockResponse(429, {}, {"Retry-After": "0"})
        if self.scenario == "rate_limit_long":
            return MockResponse(429, {}, {"Retry-After": "60"})
        if self.scenario == "server_error":
            return MockResponse(503, {})
        if self.scenario == "invalid_json":
            r = MockResponse(200)
            r.json = lambda: (_ for _ in ()).throw(ValueError("bad"))
            return r
        return MockResponse(200, fixtures()[self.platform])


def mock_sync(platform, scenario="ok", journal=None):
    if platform not in BASES:
        raise MarketplaceHTTPError("unsupported_provider")
    if scenario not in {"ok", "unauthorized", "rate_limit", "rate_limit_long",
                        "server_error", "invalid_json"}:
        raise MarketplaceHTTPError("unsupported_scenario")
    # Synthetic credentials never leave the process: MockTransport sends no HTTP.
    c = Credentials(token="MOCK-NEVER-SEND", client_id="MOCK", business_id=123)
    transport = MockTransport(platform, scenario)
    result = _perform(platform, "GRIT-DEMO-SELLER", c, transport,
                      LocalRateLimiter(sleep=lambda _: None),
                      lambda _: None, journal or JOURNAL, "mock")
    result["http_calls"] = len(transport.calls)
    result["no_real_network"] = True
    return result


def authorized_read_once(platform, seller, *, transport=None, journal=None):
    """Opt-in library function, not exposed as a web route or scheduled job.

    Enabling the flag alone NEVER triggers outbound requests.
    """
    if os.getenv("GRIT_MARKETPLACE_LIVE_READ_ENABLED") != "1":
        raise MarketplaceHTTPError("live_read_disabled")
    credentials = load_authorized_credentials(platform)
    return _perform(platform, seller, credentials,
                    transport if transport is not None else requests.Session(),
                    THROTTLE, time.sleep, journal or JOURNAL, "live_read_only")
