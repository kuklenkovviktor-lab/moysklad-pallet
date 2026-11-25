"""Read-only WB FBS supply membership accounting.

Supply date is the named shipment date (when an exact 'Поставка от DD.MM.YYYY'
label is present), otherwise WB shippingDt. NEVER use an order's createdAt
or its current supplierStatus to decide which day's supply it belongs to.
Only WB's official supply membership defines the denominator.
"""
import hashlib
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import quote

import requests

API = "https://marketplace-api.wildberries.ru"
LABEL_DAY = re.compile(r"^Поставка от (\d{2})\.(\d{2})\.(\d{4})$")
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUPPLY_ID = re.compile(r"^WB-GI-[0-9]+$")
_CACHE = {}
_LOCK = threading.Lock()
CACHE_SECONDS = 90


class SupplyProgressError(RuntimeError):
    pass


def valid_day(day):
    if not isinstance(day, str) or not DAY.fullmatch(day):
        return False
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
    except ValueError:
        return False


def shipment_day(supply):
    name = str(supply.get("name") or "").strip()
    match = LABEL_DAY.fullmatch(name)
    if match:
        day = "%s-%s-%s" % (match.group(3), match.group(2), match.group(1))
        if valid_day(day):
            return day, "name"
    shipping = str(supply.get("shippingDt") or "").strip()
    if valid_day(shipping):
        return shipping, "shippingDt"
    return None, "unknown"


class WBReadOnlySupplyClient:
    """GET only. Does not submit or change any order, supply or WB status."""

    def __init__(self, token, get=None):
        self.token = str(token or "").strip()
        if not self.token:
            raise SupplyProgressError("WB API token unavailable")
        self.get = get or requests.get

    def _fetch(self, path, params=None):
        try:
            response = self.get(
                API + path,
                headers={"Authorization": self.token, "Accept": "application/json"},
                params=params, timeout=12, allow_redirects=False,
            )
            response.raise_for_status()
            value = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise SupplyProgressError("WB supply read failed") from exc
        if not isinstance(value, dict):
            raise SupplyProgressError("WB returned invalid supply data")
        return value

    def supplies(self):
        result = []
        seen_ids, cursors = set(), set()
        cursor = 0
        for _ in range(20):
            if cursor in cursors:
                raise SupplyProgressError("WB supply pagination repeated")
            cursors.add(cursor)
            payload = self._fetch("/api/v3/supplies", {"limit": 1000, "next": cursor})
            rows = payload.get("supplies")
            if not isinstance(rows, list):
                raise SupplyProgressError("WB supply list missing")
            for row in rows:
                if not isinstance(row, dict) or not SUPPLY_ID.fullmatch(str(row.get("id") or "")):
                    raise SupplyProgressError("WB supply ID invalid")
                if row["id"] in seen_ids:
                    raise SupplyProgressError("WB supplied duplicate supply ID")
                seen_ids.add(row["id"])
                result.append(row)
            nxt = payload.get("next")
            if not rows or nxt in (None, 0, "0"):
                return result
            if type(nxt) is not int or nxt < 0:
                raise SupplyProgressError("WB supply cursor invalid")
            cursor = nxt
        raise SupplyProgressError("WB supplies pagination incomplete")

    def supply_order_ids(self, supply_id):
        if not SUPPLY_ID.fullmatch(str(supply_id or "")):
            raise SupplyProgressError("WB supply ID invalid")
        payload = self._fetch(
            "/api/marketplace/v3/supplies/" + quote(supply_id, safe="") + "/order-ids"
        )
        values = payload.get("orderIds")
        if not isinstance(values, list):
            raise SupplyProgressError("WB supply order IDs missing")
        ids = set()
        for raw in values:
            if type(raw) is not int or raw <= 0:
                raise SupplyProgressError("WB returned invalid order ID")
            oid = str(raw)
            if oid in ids:
                raise SupplyProgressError("WB returned duplicate order ID")
            ids.add(oid)
        return ids


def supply_snapshot(day, client):
    """Get complete membership for ONE named/shipping day; fail on ambiguity."""
    if not valid_day(day):
        raise SupplyProgressError("Invalid day")
    supplies = []
    all_ids = set()
    unclassified = 0
    for supply in client.supplies():
        date_key, basis = shipment_day(supply)
        if date_key is None:
            unclassified += 1
            continue
        if date_key != day:
            continue
        sid = supply["id"]
        ids = client.supply_order_ids(sid)
        if all_ids.intersection(ids):
            raise SupplyProgressError("WB order belongs to multiple supplies on the day")
        all_ids.update(ids)
        supplies.append({"id": sid, "name": str(supply.get("name") or ""),
                         "day_source": basis, "count": len(ids)})
    if not supplies:
        raise SupplyProgressError("No verified WB supplies found for selected day")
    if unclassified:
        # An undated supply might belong to this day, so a complete
        # authoritative count cannot be claimed.
        raise SupplyProgressError("Some WB supplies have no verifiable day")
    return {"day": day, "supplies": supplies, "order_ids": all_ids}


def cached_snapshot(day, token):
    """Cache ONLY WB membership, never GRIT scans. Fresh scans are read per request."""
    key = (day, hashlib.sha256(token.encode("utf-8")).hexdigest())
    now = time.monotonic()
    with _LOCK:
        item = _CACHE.get(key)
        if item and now - item[0] < CACHE_SECONDS:
            return item[1]
        value = supply_snapshot(day, WBReadOnlySupplyClient(token))
        _CACHE[key] = (time.monotonic(), value)
        return value


def _chunks(ids, size=300):
    values = sorted(ids)
    for i in range(0, len(values), size):
        yield values[i:i + size]


def calculate_supply_progress(snapshot, account, query_all):
    """Exact set difference. No modifications to WB, scans, queue or orders."""
    order_ids = snapshot["order_ids"]
    scanned = set()
    known = set()
    admitted = set()
    for chunk in _chunks(order_ids):
        marks = ",".join("?" for _ in chunk)
        # A confirmed GRIT scan is the only evidence for 'collected'.
        scanned.update(str(r[0]) for r in query_all(
            "SELECT DISTINCT CAST(wb_order_id AS TEXT) FROM scans "
            "WHERE marketplace='WB' AND wb_order_id IN (" + marks + ")",
            tuple(chunk)))
        # Backwards compatibility: older PDF scans can lack wb_order_id,
        # yet have the exact sticker linked to an API order in the same account.
        scanned.update(str(r[0]) for r in query_all(
            "SELECT DISTINCT t.order_id FROM wb_fbs_stickers t "
            "JOIN scans s ON s.barcode=t.barcode AND s.marketplace='WB' "
            "WHERE t.account_id=? AND t.order_id IN (" + marks + ")",
            tuple([account] + chunk)))
        known.update(str(r[0]) for r in query_all(
            "SELECT order_id FROM wb_fbs_orders WHERE account_id=? "
            "AND order_id IN (" + marks + ")",
            tuple([account] + chunk)))
        admitted.update(str(r[0]) for r in query_all(
            "SELECT order_id FROM wb_fbs_picking_queue WHERE account_id=? "
            "AND order_id IN (" + marks + ")",
            tuple([account] + chunk)))
    scanned &= order_ids
    known &= order_ids
    admitted &= order_ids
    remaining = order_ids - scanned
    return {
        "state": "connected", "day": snapshot["day"], "basis": "wb_supply_membership",
        "supplies": snapshot["supplies"], "supply_count": len(snapshot["supplies"]),
        "supply_total": len(order_ids), "scanned_orders": len(scanned),
        "remaining_orders": len(remaining),
        "missing_in_grit": len(remaining - known),
        "not_admitted_to_queue": len((remaining & known) - admitted),
    }
