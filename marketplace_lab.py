"""GRIT WMS integration lab: deterministic, isolated marketplace API contract tests.

No credentials, database writes, network calls, shipping/status mutations or background
jobs. All records are synthetic and never enter production picking.
The adapters normalize selected documented fields, not full API compatibility.
"""
from copy import deepcopy

PLATFORMS = ("wildberries", "ozon", "yandex")
LAB_TENANT = "GRIT-LAB-ONLY"
SOURCES = {
    "wildberries": "GET /api/v3/orders/new",
    "ozon": "POST /v4/posting/fbs/list",
    "yandex": "POST /v1/businesses/{businessId}/orders",
}
DOCUMENTATION = {
    "wildberries": "https://dev.wildberries.ru/openapi/orders-fbs",
    "ozon": "https://dev.ozon.ru/news/734-Otkliuchenie-starykh-metodov-Seller-API-dlia-raboty-s-postingami/",
    "yandex": "https://yandex.ru/dev/market/partner-api/doc/ru/reference/orders/getBusinessOrders",
}


class AdapterError(ValueError):
    pass


def _text(value, label):
    result = str(value if value is not None else "").strip()
    if not result or len(result) > 255:
        raise AdapterError("Отсутствует или некорректно поле: " + label)
    return result


def _qty(value):
    if isinstance(value, bool) or isinstance(value, float) and not value.is_integer():
        raise AdapterError("Количество должно быть целым положительным числом")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise AdapterError("Количество должно быть целым положительным числом") from None
    if not 1 <= number <= 100000:
        raise AdapterError("Количество должно быть от 1 до 100000")
    return number


def _item(article, barcode, quantity):
    return {
        "article": _text(article, "артикул"),
        "barcode": str(barcode or "").strip()[:255],  # Barcode may not be supplied by API.
        "quantity": _qty(quantity),
    }


def _order(platform, seller, external_id, status, items):
    if not items:
        raise AdapterError("В заказе нет товарных позиций")
    if len(items) > 500:
        raise AdapterError("Слишком много товарных позиций")
    return {
        "key": "|".join((platform, _text(seller, "seller_id"), _text(external_id, "order_id"))),
        "platform": platform,
        "seller_id": seller,
        "external_id": str(external_id),
        "status": _text(status, "статус"),
        "items": items,
        "total_units": sum(item["quantity"] for item in items),
        "source": "synthetic_fixture",
    }


def _list(value, name):
    if not isinstance(value, list):
        raise AdapterError("Некорректный массив: " + name)
    return value


def normalize_wb(raw, seller):
    """WB FBS: each assembly task represents one unit; orderUid is not the task ID."""
    orders = _list(raw.get("orders"), "orders")
    result = []
    for r in orders:
        if not isinstance(r, dict):
            raise AdapterError("Некорректное задание WB")
        article = r.get("article") or r.get("supplierArticle")
        barcodes = r.get("skus") or []
        barcode = barcodes[0] if isinstance(barcodes, list) and barcodes else ""
        result.append(_order("wildberries", seller, r.get("id"), "new", [
            _item(article, barcode, 1),
        ]))
    return result


def normalize_ozon(raw, seller):
    """Selected FBS posting fields; synthetic v4-shaped fixture, not a live API call."""
    body = raw.get("result", raw)
    if not isinstance(body, dict):
        raise AdapterError("Некорректный ответ Ozon")
    postings = _list(body.get("postings"), "postings")
    result = []
    for posting in postings:
        if not isinstance(posting, dict):
            raise AdapterError("Некорректное отправление Ozon")
        products = _list(posting.get("products"), "products")
        items = [_item(p.get("offer_id"), p.get("barcode"), p.get("quantity"))
                 for p in products if isinstance(p, dict)]
        if len(items) != len(products):
            raise AdapterError("Некорректная товарная позиция Ozon")
        result.append(_order("ozon", seller, posting.get("posting_number"),
                             posting.get("status") or "unknown", items))
    return result


def normalize_yandex(raw, seller):
    """Business orders endpoint: use orderId, offerId, count and explicit status."""
    orders = _list(raw.get("orders"), "orders")
    result = []
    for order in orders:
        if not isinstance(order, dict):
            raise AdapterError("Некорректный заказ Яндекс Маркета")
        products = _list(order.get("items"), "items")
        items = [_item(p.get("offerId"), p.get("barcode"), p.get("count"))
                 for p in products if isinstance(p, dict)]
        if len(items) != len(products):
            raise AdapterError("Некорректная товарная позиция Яндекс Маркета")
        result.append(_order("yandex", seller, order.get("orderId"),
                             order.get("status") or "unknown", items))
    return result


NORMALIZERS = {
    "wildberries": normalize_wb,
    "ozon": normalize_ozon,
    "yandex": normalize_yandex,
}


def fixtures():
    """Artificial orders intentionally use overlapping SKUs across sellers."""
    return {
        "wildberries": {
            "orders": [
                {"id": 10101, "orderUid": "demo-basket-1", "article": "GRIT-001", "skus": ["4600000000011"]},
                {"id": 10102, "orderUid": "demo-basket-1", "article": "GRIT-001", "skus": ["4600000000011"]},
            ],
        },
        "ozon": {
            "result": {"postings": [{
                "posting_number": "DEMO-POST-1001", "status": "awaiting_packaging",
                "products": [
                    {"offer_id": "GRIT-001", "quantity": 2, "barcode": "4600000000011"},
                    {"offer_id": "GRIT-002", "quantity": 1, "barcode": "4600000000028"},
                ],
            }]},
        },
        "yandex": {
            "orders": [{
                "orderId": 30301, "campaignId": 555, "programType": "FBS",
                "status": "PROCESSING", "fake": True,
                "items": [
                    {"offerId": "GRIT-001", "count": 1},
                    {"offerId": "GRIT-003", "count": 3},
                ],
            }],
        },
    }


def _collect(platform, seller, payload, ledger):
    current = NORMALIZERS[platform](payload, seller)
    added = 0
    for order in current:
        if order["key"] not in ledger:
            ledger[order["key"]] = order
            added += 1
        elif ledger[order["key"]] != order:
            # A changed marketplace state updates the laboratory view only.
            ledger[order["key"]] = order
    return added


def run_scenario(name):
    if name not in ("normal", "duplicates", "isolation", "invalid", "rate_limit"):
        raise AdapterError("Неизвестный сценарий")
    ledger = {}
    originals = fixtures()
    details = []
    if name == "rate_limit":
        return {
            "scenario": name, "passed": True, "orders": [], "total_units": 0,
            "checks": ["HTTP 429: повтор отложен, без обращения к реальным API",
                       "Retry-After: задержка должна соблюдаться будущим сетевым адаптером"],
            "attempts": 0, "mode": "offline_only",
        }
    if name == "invalid":
        bad = deepcopy(originals["ozon"])
        bad["result"]["postings"][0]["products"][0]["quantity"] = -3
        try:
            _collect("ozon", "SELLER-A", bad, ledger)
        except AdapterError as exc:
            return {
                "scenario": name, "passed": len(ledger) == 0,
                "orders": [], "total_units": 0, "checks": [str(exc)],
                "mode": "offline_only",
            }
        raise AssertionError("Некорректный товар не был отклонён")
    for platform in PLATFORMS:
        count = _collect(platform, "SELLER-A", originals[platform], ledger)
        details.append(platform + ": загружено " + str(count))
    expected = 4
    if name == "duplicates":
        for platform in PLATFORMS:
            count = _collect(platform, "SELLER-A", originals[platform], ledger)
            details.append(platform + ": повторно добавлено " + str(count))
        passed = len(ledger) == expected
    elif name == "isolation":
        count = _collect("wildberries", "SELLER-B", originals["wildberries"], ledger)
        details.append("SELLER-B: независимо загружено " + str(count))
        passed = len(ledger) == expected + 2
    else:
        passed = len(ledger) == expected
    orders = list(ledger.values())
    if len({o["key"] for o in orders}) != len(orders):
        passed = False
    return {
        "scenario": name, "passed": passed,
        "orders": orders, "total_units": sum(o["total_units"] for o in orders),
        "checks": details, "mode": "offline_only",
    }
