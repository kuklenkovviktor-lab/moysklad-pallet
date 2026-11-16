"""Fail-closed Ozon FBO preflight against MoySklad product articles.

Only reads product cards. Never alters MoySklad products, stock or orders.
"""
import hashlib
import json
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

API_BASE = "https://api.moysklad.ru/api/remap/1.2"

# All Ozon FBO preflight requests share one pacing gate per Gunicorn worker.
# Current user-token quota may be lower than the account-level documented cap.
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0
_MIN_REQUEST_SPACING = 0.35


def _wait_for_catalog_slot():
    global _NEXT_REQUEST_AT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _NEXT_REQUEST_AT - now)
        _NEXT_REQUEST_AT = max(_NEXT_REQUEST_AT, now) + _MIN_REQUEST_SPACING
    if wait:
        time.sleep(wait)




class CatalogUnavailable(ValueError):
    pass


def decision_key(article, barcode):
    raw = json.dumps([article, barcode], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _barcodes(product):
    """Only product unit barcodes, not pallet/packaging codes."""
    result = set()
    for value in product.get("barcodes") or []:
        if isinstance(value, str):
            result.add(value.strip())
        elif isinstance(value, dict):
            for key in ("ean13", "ean8", "code128", "gtin", "upc", "barcode"):
                if value.get(key):
                    result.add(str(value[key]).strip())
    return sorted(x for x in result if x)


def lookup_article(article):
    """Read and validate one article with bounded transient-error retries.

    Diagnostic never includes token, response body or upstream URL.
    """
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if not token:
        raise CatalogUnavailable("API МойСклад не настроен. Импорт заблокирован.")
    article = str(article).strip()
    for attempt in range(3):
        try:
            _wait_for_catalog_slot()
            response = requests.get(
                API_BASE + "/entity/product",
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                params={"filter": "article=" + article, "limit": 100},
                timeout=12,
            )
        except requests.Timeout as exc:
            if attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise CatalogUnavailable(
                "МойСклад: тайм-аут проверки артикула " + article[:80] + " (3 попытки)."
            ) from exc
        except requests.RequestException as exc:
            raise CatalogUnavailable(
                "МойСклад: ошибка сети при проверке артикула " + article[:80] + "."
            ) from exc
        # The same exact filter succeeds standalone but sporadically fails
        # in bulk. Retry a 400 at most twice with pause; a persistent 400
        # still fails closed and is never treated as a valid product.
        if response.status_code in (400, 429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(0.5 * (attempt + 1))
            continue
        if response.status_code == 400:
            # Some MoySklad product filters reject particular article syntax.
            # Fall back to catalog search, then match article exactly below.
            # Never accept a fuzzy match or silently replace any barcode.
            try:
                _wait_for_catalog_slot()
                response = requests.get(
                    API_BASE + "/entity/product",
                    headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                    params={"search": article, "limit": 100},
                    timeout=12,
                )
            except requests.Timeout as exc:
                raise CatalogUnavailable(
                    "МойСклад: тайм-аут резервного поиска артикула " + article[:80] + "."
                ) from exc
            except requests.RequestException as exc:
                raise CatalogUnavailable(
                    "МойСклад: ошибка сети резервного поиска артикула " + article[:80] + "."
                ) from exc
            if not response.ok:
                raise CatalogUnavailable(
                    "МойСклад: фильтр HTTP 400, резервный поиск HTTP " +
                    str(response.status_code) + " для артикула " + article[:80] + "."
                )
        elif not response.ok:
            raise CatalogUnavailable(
                "МойСклад HTTP " + str(response.status_code) +
                " при проверке артикула " + article[:80] + "."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatalogUnavailable(
                "МойСклад: некорректный JSON для артикула " + article[:80] + "."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise CatalogUnavailable(
                "МойСклад: неожиданный формат каталога для артикула " + article[:80] + "."
            )
        candidates = [row for row in payload["rows"]
                      if isinstance(row, dict) and
                      str(row.get("article") or "").strip().casefold() == article.casefold()]
        if len(candidates) != 1:
            return {"status": "missing" if not candidates else "ambiguous", "barcodes": []}
        codes = _barcodes(candidates[0])
        return {"status": "found" if codes else "no_barcodes", "barcodes": codes}
    raise CatalogUnavailable("МойСклад: проверка артикула не завершена.")



def check_catalog(plan, lookup=lookup_article):
    """Return only mismatches, one per distinct article+barcode pair."""
    pairs = {}
    for cargo in plan["cargo"]:
        for item in cargo["items"]:
            pair = (item["article"], item["barcode"])
            pairs.setdefault(pair, {"cargo": set(), "sources": set()})
            pairs[pair]["cargo"].add(cargo["barcode"])
            pairs[pair]["sources"].add(item["source"])
    articles = sorted({article for article, _ in pairs})
    if len(articles) > 500:
        raise CatalogUnavailable("Слишком много уникальных артикулов для одной проверки (максимум 500).")
    # Limit both concurrency and request rate. Four simultaneous requests
    # passed a one-off test but bulk imports hit intermittent HTTP 400.
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            matches = dict(zip(articles, pool.map(lookup, articles)))
    except CatalogUnavailable:
        raise
    except Exception as exc:
        raise CatalogUnavailable("Ошибка сверки МойСклад. Импорт заблокирован.") from exc
    issues = []
    for (article, barcode), info in pairs.items():
        match = matches[article]
        codes = match["barcodes"]
        if match["status"] == "found" and barcode in codes:
            continue
        issues.append({
            "key": decision_key(article, barcode),
            "article": article, "excel_barcode": barcode,
            "barcodes": codes if match["status"] == "found" else [],
            "reason": match["status"] if match["status"] != "found" else "mismatch",
            "cargo_barcodes": sorted(info["cargo"]),
            "sources": sorted(info["sources"]),
        })
    return sorted(issues, key=lambda x: (x["article"], x["excel_barcode"]))


def resolved_manifest(plan, issues, decisions, excluded):
    """Apply manager choices to a parsed copy, preserving whole cargo integrity."""
    if not isinstance(decisions, dict) or not isinstance(excluded, list):
        raise ValueError("Некорректные решения по расхождениям.")
    cargo_ids = {c["barcode"] for c in plan["cargo"]}
    if any(not isinstance(x, str) or x not in cargo_ids for x in excluded):
        raise ValueError("Неизвестное грузоместо для исключения.")
    excluded_set = set(excluded)
    issue_by_pair = {(x["article"], x["excel_barcode"]): x for x in issues}
    valid_keys = {x["key"] for x in issues}
    if set(decisions) - valid_keys:
        raise ValueError("Решения не соответствуют текущей проверке. Проверь файл снова.")
    resolved, audit = [], []
    barcode_article = {}
    for cargo in plan["cargo"]:
        if cargo["barcode"] in excluded_set:
            continue
        items = []
        for item in cargo["items"]:
            old = item["barcode"]
            mismatch = issue_by_pair.get((item["article"], old))
            barcode = old
            if mismatch:
                barcode = decisions.get(mismatch["key"])
                if barcode not in mismatch["barcodes"]:
                    raise ValueError("Не все расхождения устранены. Исправь ШК либо исключи всё грузоместо.")
                audit.append({
                    "cargo": cargo["barcode"], "article": item["article"],
                    "source": item["source"], "old": old, "new": barcode
                })
            previous = barcode_article.get(barcode)
            if previous and previous.casefold() != item["article"].casefold():
                raise ValueError("Один ШК назначен разным артикулам. Проверь исправления.")
            barcode_article[barcode] = item["article"]
            copy = dict(item)
            copy["barcode"] = barcode
            items.append(copy)
        # Consolidate identical article + corrected barcode, but do not silently double count.
        seen = set()
        for item in items:
            key = (item["article"].casefold(), item["barcode"])
            if key in seen:
                raise ValueError("После исправления появились две строки одного товара в грузоместе. Исправь исходный файл.")
            seen.add(key)
        clone = dict(cargo)
        clone["items"] = items
        clone["qty"] = sum(int(x["qty"]) for x in items)
        resolved.append(clone)
    if not resolved:
        raise ValueError("Нельзя создать поставку без грузомест.")
    return resolved, audit


def resolved_plan_hash(cargo):
    signature = [(p["barcode"], p["destination"], p["label"],
                  sorted((i["barcode"], i["article"], i["qty"]) for i in p["items"]))
                 for p in cargo]
    return hashlib.sha256(
        json.dumps(sorted(signature), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
