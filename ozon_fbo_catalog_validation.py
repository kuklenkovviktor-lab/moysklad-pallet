"""Fail-closed Ozon FBO preflight against MoySklad product articles.

Only reads product cards. Never alters MoySklad products, stock or orders.
"""
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import requests

API_BASE = "https://api.moysklad.ru/api/remap/1.2"


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
        for attempt in range(3):
            try:
                response = requests.get(endpoint, headers=headers, params=params, timeout=12)
            except requests.Timeout as exc:
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                stage = "резервного поиска артикула " if fallback else "проверки артикула "
                raise CatalogUnavailable(
                    "МойСклад: тайм-аут " + stage + article[:80] + " (3 попытки)."
                ) from exc
            except requests.RequestException as exc:
                stage = "резервного поиска артикула " if fallback else "проверки артикула "
                raise CatalogUnavailable(
                    "МойСклад: ошибка сети " + stage + article[:80] + "."
                ) from exc
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (attempt + 1))
                continue
            return response
        raise CatalogUnavailable("МойСклад: проверка артикула не завершена.")

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
    # Four workers minimize latency without flooding MoySklad with requests.
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
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
