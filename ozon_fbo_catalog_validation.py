"""Fail-closed Ozon FBO preflight against MoySklad product articles.

Only reads product cards. Never alters MoySklad products, stock or orders.
"""
import hashlib
import json
import logging
import os
import re
import time
import threading

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
    values = product.get("barcodes", [])
    if not isinstance(values, list):
        raise CatalogUnavailable("МойСклад вернул некорректный список ШК.")
    for value in values:
        if isinstance(value, str):
            result.add(value.strip())
        elif isinstance(value, dict):
            for key in ("ean13", "ean8", "code128", "gtin", "upc", "barcode"):
                if key in value:
                    if not isinstance(value[key], str):
                        raise CatalogUnavailable("МойСклад вернул некорректный ШК.")
                    result.add(value[key].strip())
        else:
            raise CatalogUnavailable("МойСклад вернул некорректный формат ШК.")
    return sorted(x for x in result if x)


class _FilterRejected(CatalogUnavailable):
    pass


def _error_code(response):
    try:
        code = response.json()["errors"][0]["code"]
    except (ValueError, TypeError, KeyError, IndexError):
        return ""
    return f" (код {code})" if type(code) is int else ""


ENTITY_TYPES = ("product", "bundle")


def _read_rows(params, context, entity):
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if not token:
        raise CatalogUnavailable("API МойСклад не настроен. Импорт заблокирован.")
    headers = {"Authorization": "Bearer " + token,
               "Accept": "application/json;charset=utf-8", "Accept-Encoding": "gzip"}
    for attempt in range(3):
        _wait_for_catalog_slot()
        try:
            response = requests.get(API_BASE + "/entity/" + entity, headers=headers,
                                    params={**params, "limit": 100}, timeout=12,
                                    allow_redirects=False)
        except requests.Timeout as exc:
            if attempt == 2:
                raise CatalogUnavailable(f"МойСклад: тайм-аут ({context}).") from exc
            time.sleep(0.5 * (attempt + 1))
            continue
        except requests.RequestException as exc:
            raise CatalogUnavailable(f"МойСклад: ошибка сети ({context}).") from exc
        status = response.status_code
        if status == 200:
            break
        error = f"МойСклад HTTP {status}{_error_code(response)} ({context})."
        if status == 400:
            logging.getLogger(__name__).warning("Ozon FBO %s", error)
        if status in (400, 429, 500, 502, 503, 504) and attempt < 2:
            delay = 0.5 * (attempt + 1)
            if status == 429:
                for name, scale in (("X-Lognex-Retry-After", 1000), ("Retry-After", 1)):
                    value = response.headers.get(name)
                    if isinstance(value, str) and value.isascii() and value.isdecimal():
                        delay = max(delay, float(value) / scale)
                if delay > 12:
                    raise CatalogUnavailable(error)
            time.sleep(delay)
            continue
        if status == 400 and "filter" in params:
            raise _FilterRejected(error)
        raise CatalogUnavailable(error)
    try:
        payload = response.json()
    except ValueError as exc:
        raise CatalogUnavailable(f"МойСклад: некорректный JSON ({context}).") from exc
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise CatalogUnavailable(f"МойСклад: неожиданный формат каталога ({context}).")
    meta = payload.get("meta", {})
    if (not isinstance(meta, dict) or meta.get("nextHref") or
            meta.get("size", len(rows)) != len(rows) or
            (len(rows) >= 100 and "size" not in meta)):
        raise CatalogUnavailable(f"МойСклад: неполная выборка каталога ({context}).")
    for row in rows:
        row_meta = row.get("meta", {})
        if not isinstance(row_meta, dict) or row_meta.get("type", entity) != entity:
            raise CatalogUnavailable("МойСклад вернул карточку другого типа.")
    return rows


def _match(article, rows):
    candidates = [row for row in rows if str(row.get("article") or "").strip().casefold()
                  == article.casefold()]
    if len(candidates) != 1:
        return {"status": "missing" if not candidates else "ambiguous", "barcodes": []}
    codes = _barcodes(candidates[0])
    return {"status": "found" if codes else "no_barcodes", "barcodes": codes}


def _filter(articles):
    return {"filter": ";".join("article=" + article.replace(";", r"\;") for article in articles)}


def _article_rows(articles, entity):
    try:
        return _read_rows(_filter(articles), articles[0], entity)
    except _FilterRejected as rejected:
        if len(articles) > 1:
            return [row for article in articles for row in _article_rows([article], entity)
                    if str(row.get("article") or "").strip().casefold() == article.casefold()]
        try:
            return _read_rows({"search": articles[0]}, articles[0], entity)
        except CatalogUnavailable as exc:
            raise CatalogUnavailable(f"{rejected} резервный поиск {str(exc).removeprefix('МойСклад ')}") from exc


def lookup_article(article):
    article = str(article).strip()
    return _bulk_matches([article])[article]


def _bulk_matches(articles):
    matches = {}
    for i in range(0, len(articles), 20):
        group = articles[i:i + 20]
        rows = [row for entity in ENTITY_TYPES for row in _article_rows(group, entity)]
        matches.update((article, _match(article, rows)) for article in group)
    return matches


def catalog_options(value, by_article=False):
    """Return typed card/identifier options; code never becomes a unit barcode."""
    if not isinstance(value, str) or not value or len(value) > 160:
        raise ValueError("Некорректный запрос каталога.")
    if not by_article and not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", value):
        raise ValueError("Некорректный идентификатор для ШК GRIT.")
    options = []
    for entity in ENTITY_TYPES:
        fields = ("article",) if by_article else ("barcode", "code")
        for field in fields:
            if by_article:
                rows = _article_rows([value], entity)
            else:
                try:
                    rows = _read_rows({"filter": field + "=" + value}, value, entity)
                except _FilterRejected:
                    rows = _read_rows({"search": value}, value, entity)
            for row in rows:
                article = row.get("article") or ""
                if not isinstance(article, str):
                    raise CatalogUnavailable("МойСклад вернул некорректный артикул.")
                article = article.strip()
                if not article or (by_article and article.casefold() != value.casefold()):
                    continue
                identifiers = [("barcode", code) for code in _barcodes(row)]
                if row.get("code"):
                    identifiers.append(("code", row["code"]))
                for kind, code in identifiers:
                    if not isinstance(code, str):
                        raise CatalogUnavailable("МойСклад вернул некорректный идентификатор.")
                    if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", code):
                        continue
                    if not by_article and (field != kind or code != value):
                        continue
                    if not isinstance(row.get("id"), str) or not row["id"]:
                        raise CatalogUnavailable("МойСклад вернул карточку без идентификатора.")
                    options.append({"id": row["id"], "entity_type": entity, "identifier_type": kind,
                                    "article": article, "value": code, "name": row.get("name") or ""})
    return options


def check_catalog(plan, lookup=None):
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
    try:
        matches = (_bulk_matches(articles) if lookup is None else
                   {article: lookup(article) for article in articles})
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
    verified = {}
    cards = {}
    for cargo in plan["cargo"]:
        if cargo["barcode"] in excluded_set:
            continue
        items = []
        for item in cargo["items"]:
            old = item["barcode"]
            mismatch = issue_by_pair.get((item["article"], old))
            barcode = old
            article = item["article"]
            if mismatch:
                key = mismatch["key"]
                if key not in verified:
                    choice = decisions.get(key)
                    if not isinstance(choice, dict) or choice.get("confirmed") is not True:
                        raise ValueError("Подтверди исправление каждой пары артикул / ШК.")
                    values = [choice.get(field, "") for field in ("article", "barcode", "id")]
                    if not all(isinstance(value, str) for value in values):
                        raise ValueError("Некорректное исправление.")
                    target, code, card_id = (value.strip() for value in values)
                    target, code = target or article, code or old
                    entity, kind = choice.get("entity_type"), choice.get("identifier_type")
                    if not card_id or entity not in ENTITY_TYPES or kind not in ("code", "barcode"):
                        raise ValueError("Выбери карточку и тип идентификатора МойСклад.")
                    if code not in cards:
                        cards[code] = catalog_options(code)
                    matches = [card for card in cards[code]
                               if card["article"].casefold() == target.casefold()]
                    identities = {(card["entity_type"], card["id"]) for card in matches}
                    if identities != {(entity, card_id)}:
                        raise ValueError("Карточка изменилась или итоговая пара неоднозначна.")
                    matches = [card for card in matches if card["identifier_type"] == kind]
                    if len(matches) != 1:
                        raise ValueError("Пара артикул / ШК не найдена или неоднозначна. Выбери карточку МойСклад.")
                    verified[key] = matches[0]
                card = verified[key]
                article, barcode = card["article"], card["value"]
                audit.append({
                    "cargo": cargo["barcode"], "article": item["article"],
                    "new_article": article, "product_id": card["id"],
                    "entity_type": card["entity_type"], "identifier_type": card["identifier_type"],
                    "source": item["source"], "old": old, "new": barcode
                })
            previous = barcode_article.get(barcode)
            if previous and previous.casefold() != article.casefold():
                raise ValueError("Один ШК назначен разным артикулам. Проверь исправления.")
            barcode_article[barcode] = article
            copy = dict(item)
            copy["article"] = article
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
