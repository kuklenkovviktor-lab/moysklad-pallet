"""Fail-closed Ozon FBO preflight against MoySklad product articles.

Only reads product cards. Never alters MoySklad products, stock or orders.
"""
import hashlib
import json
import os
import time
import threading
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests
from fbs_catalog import ozon_catalog_snapshot, search_ozon_catalog_snapshot

API_BASE = "https://api.moysklad.ru/api/remap/1.2"


class CatalogUnavailable(ValueError):
    pass


# A single pacing gate across preflight, manual catalog search and product validation.
# Multiple manager requests share this gate within each application worker.
_API_LOCK = threading.Lock()
_API_NEXT_ALLOWED = 0.0
_API_MIN_INTERVAL = 0.4
_API_MAX_RETRIES = 5


def _retry_delay(value, attempt):
    """Honor Retry-After seconds or HTTP-date; otherwise bounded exponential backoff."""
    fallback = min(2.0 * (2 ** attempt), 30.0)
    if value:
        try:
            delay = float(value)
        except (TypeError, ValueError):
            try:
                stamp = parsedate_to_datetime(value)
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                delay = (stamp - datetime.now(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                return fallback
        return max(0.0, min(delay, 60.0))
    return fallback


def _catalog_get(url, *, headers, params=None, timeout=12):
    """Serialize calls and apply one shared retry policy for MoySklad read requests."""
    global _API_NEXT_ALLOWED
    for attempt in range(_API_MAX_RETRIES + 1):
        with _API_LOCK:
            delay = max(0.0, _API_NEXT_ALLOWED - time.monotonic())
            if delay:
                time.sleep(delay)
            _API_NEXT_ALLOWED = time.monotonic() + _API_MIN_INTERVAL
            try:
                response = requests.get(url, headers=headers, params=params, timeout=timeout)
            except requests.Timeout as exc:
                if attempt == _API_MAX_RETRIES:
                    raise CatalogUnavailable("МойСклад: тайм-аут при чтении каталога после повторных попыток.") from exc
                retry = _retry_delay(None, attempt)
            except requests.RequestException as exc:
                raise CatalogUnavailable("МойСклад: ошибка сети при чтении каталога.") from exc
            else:
                if response.status_code not in (429, 500, 502, 503, 504):
                    return response
                if attempt == _API_MAX_RETRIES:
                    if response.status_code == 429:
                        raise CatalogUnavailable(
                            "МойСклад ограничил частоту запросов (HTTP 429). "
                            "Проверка приостановлена, повтори позже.")
                    return response
                retry = _retry_delay(
                    response.headers.get("Retry-After") if response.status_code == 429 else None,
                    attempt)
            # Keep other application threads from immediately hammering the API.
            _API_NEXT_ALLOWED = max(_API_NEXT_ALLOWED, time.monotonic() + retry)
    raise CatalogUnavailable("МойСклад: повторные попытки завершились без ответа.")



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
    """Read all bounded catalog pages, then validate one exact article match.

    A partial search must never be reported as "missing": that would hide
    products which occur after the first MoySklad results page.
    """
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if not token:
        raise CatalogUnavailable("API МойСклад не настроен. Импорт заблокирован.")
    article = str(article).strip()
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    endpoint = API_BASE + "/entity/product"
    page_size = 100
    max_pages = 10

    def request_page(params, *, fallback=False):
        try:
            return _catalog_get(endpoint, headers=headers, params=params, timeout=12)
        except CatalogUnavailable as exc:
            # Preserve the offending article without exposing credentials.
            # Diagnostics and managers need the same context as before.
            raise CatalogUnavailable(str(exc) + " Артикул: " + article[:80] + ".") from exc

    # Only switch to search if MoySklad rejects the article filter.
    search_mode = False
    candidates = []
    for page in range(max_pages):
        params = ({"search": article} if search_mode else {"filter": "article=" + article})
        params.update({"limit": page_size, "offset": page * page_size})
        response = request_page(params, fallback=search_mode)
        if response.status_code == 400 and not search_mode:
            search_mode = True
            candidates.clear()
            params = {"search": article, "limit": page_size, "offset": 0}
            response = request_page(params, fallback=True)
        if response.status_code == 400 and search_mode:
            # Unsupported search is a discrepancy for manager review, not an absent SKU.
            return {"status": "manual_review", "barcodes": []}
        if not response.ok:
            if search_mode:
                raise CatalogUnavailable(
                    "МойСклад: резервный поиск HTTP " + str(response.status_code) +
                    " для артикула " + article[:80] + "."
                )
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
        rows = payload["rows"]
        candidates.extend(
            row for row in rows
            if isinstance(row, dict) and
            str(row.get("article") or "").strip().casefold() == article.casefold()
        )
        # Respect server-side collection size when present; otherwise the
        # first short page proves the result set is complete.
        meta = payload.get("meta")
        size = meta.get("size") if isinstance(meta, dict) else None
        completed = (isinstance(size, int) and (page + 1) * page_size >= size) or len(rows) < page_size
        if completed:
            if len(candidates) != 1:
                return {"status": "missing" if not candidates else "ambiguous", "barcodes": []}
            codes = _barcodes(candidates[0])
            return {"status": "found" if codes else "no_barcodes", "barcodes": codes}
    # Do not convert an incomplete 1000+ item response into a false absence.
    raise CatalogUnavailable(
        "МойСклад: слишком много результатов поиска артикула " + article[:80] +
        " (проверено 1000). Уточни артикул или каталог."
    )


def _snapshot_article_result(snapshot, article):
    """Fail closed on duplicate article IDs; unit codes only, never packages."""
    candidates = snapshot.get(str(article).strip().casefold())
    if not candidates:
        # The FBS snapshot may not cover all products. Do not call it 'missing'.
        return {"status": "manual_review", "barcodes": []}
    if len(candidates) != 1:
        return {"status": "ambiguous", "barcodes": []}
    codes = sorted(next(iter(candidates.values()))["barcodes"])
    return {"status": "found" if codes else "no_barcodes", "barcodes": codes}


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
    # Reuse the recent persisted FBS snapshot. No per-article MoySklad calls
    # when the snapshot is available. Unlisted articles require manager review.
    # An absent/stale snapshot is NOT trusted and falls back to live checks.
    try:
        snapshot = ozon_catalog_snapshot() if lookup is lookup_article else None
        if snapshot is None and lookup is lookup_article:
            raise CatalogUnavailable(
                "Локальный справочник МойСклад отсутствует или старше 48 часов. "
                "Обнови справочник FBS перед проверкой Ozon FBO.")
        matches = ({article: _snapshot_article_result(snapshot, article) for article in articles}
                   if snapshot is not None
                   else {article: lookup(article) for article in articles})
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
    """Apply a manager-selected MoySklad product article and barcode to a parsed copy."""
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
    resolved, audit, barcode_article = [], [], {}
    for cargo in plan["cargo"]:
        if cargo["barcode"] in excluded_set:
            continue
        items = []
        for item in cargo["items"]:
            old_article, old_barcode = item["article"], item["barcode"]
            mismatch = issue_by_pair.get((old_article, old_barcode))
            new_article, new_barcode = old_article, old_barcode
            if mismatch:
                choice = decisions.get(mismatch["key"])
                if not isinstance(choice, dict):
                    raise ValueError("Выбери карточку МойСклад для каждого расхождения.")
                new_article = str(choice.get("article") or "").strip()
                new_barcode = str(choice.get("barcode") or "").strip()
                if not new_article or not new_barcode or len(new_article) > 160 or len(new_barcode) > 64:
                    raise ValueError("Некорректная карточка МойСклад.")
                audit.append({"cargo": cargo["barcode"], "article": old_article,
                    "new_article": new_article, "source": item["source"],
                    "old": old_barcode, "new": new_barcode})
            previous = barcode_article.get(new_barcode)
            if previous and previous.casefold() != new_article.casefold():
                raise ValueError("Один ШК назначен разным артикулам. Проверь исправления.")
            barcode_article[new_barcode] = new_article
            copy = dict(item)
            copy["article"], copy["barcode"] = new_article, new_barcode
            items.append(copy)
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


def search_catalog_products(query, limit=30):
    """Read-only product candidates; fallback when MoySklad rejects search with HTTP 400."""
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    query = str(query or "").strip()
    if not token:
        raise CatalogUnavailable("API МойСклад не настроен.")
    if len(query) < 2:
        raise ValueError("Введи минимум 2 символа для поиска.")
    if len(query) > 120:
        raise ValueError("Слишком длинный поисковый запрос.")
    count = min(max(int(limit), 1), 30)
    # Use the same persisted catalog for manager search, avoiding API bursts.
    local = search_ozon_catalog_snapshot(query, count)
    if local:
        return local
    # A card may be absent from the partial FBS snapshot. Use the throttled
    # read-only API search only when local lookup produced no candidates.
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json"}
    try:
        response = _catalog_get(API_BASE + "/entity/product", headers=headers,
                                params={"search": query, "limit": count}, timeout=12)
    except requests.RequestException as exc:
        raise CatalogUnavailable("Не удалось связаться с каталогом МойСклад.") from exc

    if response.status_code == 400:
        # Search parameters can be rejected even when a product exists.
        # Inspect a bounded read-only window, without treating its absence as proof.
        rows = []
        page_size = 100
        for page in range(20):
            try:
                current = _catalog_get(
                    API_BASE + "/entity/product", headers=headers,
                    params={"limit": page_size, "offset": page * page_size}, timeout=12)
            except requests.RequestException as exc:
                raise CatalogUnavailable("Не удалось прочитать каталог МойСклад.") from exc
            if not current.ok:
                raise CatalogUnavailable("МойСклад не позволил открыть каталог (HTTP " +
                                         str(current.status_code) + ").")
            try:
                payload = current.json()
            except ValueError as exc:
                raise CatalogUnavailable("МойСклад вернул некорректный каталог.") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
                raise CatalogUnavailable("МойСклад вернул неожиданный формат каталога.")
            page_rows = payload["rows"]
            for product in page_rows:
                if not isinstance(product, dict):
                    continue
                haystack = " ".join([str(product.get("article") or ""),
                                     str(product.get("name") or ""),
                                     " ".join(_barcodes(product))]).casefold()
                if query.casefold() in haystack:
                    rows.append(product)
                    if len(rows) >= count:
                        break
            if len(rows) >= count or len(page_rows) < page_size:
                break
    elif response.ok:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatalogUnavailable("МойСклад вернул некорректный ответ каталога.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise CatalogUnavailable("МойСклад вернул неожиданный формат каталога.")
        rows = payload["rows"]
    else:
        raise CatalogUnavailable("МойСклад вернул HTTP " + str(response.status_code) +
                                 " при поиске товара.")
    return [{
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "article": str(row.get("article") or ""),
        "barcodes": _barcodes(row),
    } for row in rows if isinstance(row, dict)]



def resolve_catalog_choices(decisions, issues):
    """Re-read selected products from MoySklad: browser fields are never authoritative."""
    if not isinstance(decisions, dict):
        raise ValueError("Некорректный выбор товаров.")
    keys = {issue["key"] for issue in issues}
    if set(decisions) - keys:
        raise ValueError("Выбор не соответствует проверке файла.")
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if decisions and not token:
        raise CatalogUnavailable("API МойСклад не настроен.")
    verified = {}
    for key, choice in decisions.items():
        if not isinstance(choice, dict):
            raise ValueError("Выбери карточку товара МойСклад.")
        product_id = str(choice.get("id") or "").strip()
        barcode = str(choice.get("barcode") or "").strip()
        if not product_id or not barcode or len(product_id) > 64:
            raise ValueError("Выбери карточку из результатов поиска МойСклад.")
        try:
            response = _catalog_get(
                API_BASE + "/entity/product/" + product_id,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                timeout=12,
            )
        except requests.RequestException as exc:
            raise CatalogUnavailable("Не удалось проверить выбранную карточку МойСклад.") from exc
        if not response.ok:
            raise CatalogUnavailable("Не удалось проверить карточку МойСклад (HTTP " + str(response.status_code) + ").")
        try:
            product = response.json()
        except ValueError as exc:
            raise CatalogUnavailable("МойСклад вернул некорректную карточку.") from exc
        article = str(product.get("article") or "").strip()
        if str(product.get("id") or "") != product_id or not article or barcode not in _barcodes(product):
            raise ValueError("Выбранная карточка или ШК не подтверждены каталогом МойСклад.")
        verified[key] = {"article": article, "barcode": barcode}
    return verified
