"""WB FBS 'На сборке' read-only aggregate.

Disabled by default for privacy: never query an employer's real account merely
because WB_API_TOKEN exists. Enable only for an explicitly authorized environment.
API endpoints /api/v3/orders (paged) and /api/v3/orders/status (status of IDs).
Never downloads labels, persists seller orders or modifies WB state.
"""
import os
import threading
import time
from datetime import datetime, timezone

import requests

API = "https://marketplace-api.wildberries.ru"
_CACHE = {"at": 0.0, "result": None, "running": False, "started_at": None}
_LOCK = threading.Lock()


class WBPlanError(Exception):
    pass


def _parse_statuses(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
        raise WBPlanError("WB вернул некорректные статусы")
    found = {}
    for order in payload["orders"]:
        if not isinstance(order, dict):
            raise WBPlanError("WB вернул некорректные статусы")
        ident = order.get("id")
        if isinstance(ident, int) and not isinstance(ident, bool):
            found[ident] = order.get("supplierStatus")
    return found


def count_assembly_orders(token, get=requests.get, post=requests.post, now=None,
                          max_pages=80):
    """Fully verify a bounded 30-day search or raise instead of partial count."""
    if not token:
        raise WBPlanError("WB API не настроен")
    timestamp = int(now if now is not None else time.time())
    cursor, ids, cursors = 0, set(), set()
    headers = {"Authorization": token, "Accept": "application/json"}
    for _ in range(max_pages):
        try:
            response = get(API + "/api/v3/orders", headers=headers,
                           params={"limit": 1000, "next": cursor,
                                   "dateFrom": timestamp - 30 * 86400,
                                   "dateTo": timestamp},
                           timeout=15, allow_redirects=False)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            raise WBPlanError("WB /orders: HTTP " + str(exc.response.status_code if exc.response is not None else "?")) from exc
        except requests.RequestException as exc:
            raise WBPlanError("WB /orders: сетевая ошибка или тайм-аут") from exc
        except ValueError as exc:
            raise WBPlanError("WB вернул некорректные данные") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            raise WBPlanError("WB вернул некорректный список заказов")
        orders = payload["orders"]
        for order in orders:
            if not isinstance(order, dict) or type(order.get("id")) is not int:
                raise WBPlanError("WB вернул заказ без ID")
            ids.add(order["id"])
        nxt = payload.get("next")
        if not orders or nxt in (None, 0, "0"):
            break
        if nxt == cursor or str(nxt) in cursors:
            raise WBPlanError("Ошибка пагинации WB")
        cursors.add(str(nxt))
        cursor = nxt
        time.sleep(0.24)  # WB minimum interval between assembly API requests
    else:
        raise WBPlanError("Слишком много заказов: полный подсчёт не завершён")

    confirmed = 0
    active_ids = []
    all_statuses = {}
    ordered_ids = sorted(ids)
    for start in range(0, len(ordered_ids), 1000):
        if start:
            time.sleep(0.24)
        chunk = ordered_ids[start:start + 1000]
        try:
            response = post(API + "/api/v3/orders/status",
                            headers={**headers, "Content-Type": "application/json"},
                            json={"orders": chunk}, timeout=15, allow_redirects=False)
            response.raise_for_status()
            states = _parse_statuses(response.json())
        except requests.HTTPError as exc:
            raise WBPlanError("WB /orders/status: HTTP " + str(exc.response.status_code if exc.response is not None else "?")) from exc
        except requests.RequestException as exc:
            raise WBPlanError("WB /orders/status: сетевая ошибка или тайм-аут") from exc
        except ValueError as exc:
            raise WBPlanError("WB /orders/status: некорректный ответ") from exc
        if len(states) != len(chunk) or not all(x in states for x in chunk):
            raise WBPlanError("WB вернул не все статусы: план не обновлён")
        all_statuses.update(states)
        on_assembly = [ident for ident in chunk if states[ident] == "confirm"]
        confirmed += len(on_assembly)
        active_ids.extend(on_assembly)
    return {"assembly_orders": confirmed, "active_order_ids": active_ids,
            "order_statuses": all_statuses,
            "checked_orders": len(ids),
            "window_days": 30,
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "WB FBS supplierStatus=confirm"}


def _refresh_assembly_plan(token):
    """Slow WB round-trip runs outside a manager HTTP request."""
    try:
        result = {"state": "connected", "read_only": True,
                  **count_assembly_orders(token)}
    except WBPlanError as exc:
        result = {"state": "error", "read_only": True,
                  "details": str(exc)}
    except Exception:
        result = {"state": "error", "read_only": True,
                  "details": "Неожиданная ошибка синхронизации WB"}
    with _LOCK:
        _CACHE.update(at=time.monotonic(), result=result, running=False,
                      started_at=None)


def assembly_plan_status():
    """Schedule a slow upstream refresh without blocking the dashboard."""
    if os.getenv("GRIT_WB_ASSEMBLY_PLAN_LIVE", "0") != "1":
        return {"state": "disabled", "read_only": True,
                "details": "Автоподсчёт WB выключен в конфигурации"}
    token = os.getenv("WB_API_TOKEN", "").strip()
    if not token:
        return {"state": "unconfigured", "read_only": True}
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE["result"]
        if cached is not None and now - _CACHE["at"] < 180:
            return dict(cached)
        if _CACHE["running"]:
            # Stale worker protection after a hung upstream call.
            if _CACHE.get("started_at") and now - _CACHE["started_at"] < 300:
                if cached and cached.get("state") == "connected":
                    return {**cached, "refreshing": True}
                return {"state": "syncing", "read_only": True,
                        "details": "Получаем статусы заказов WB"}
            _CACHE["running"] = False
        _CACHE["running"] = True
        _CACHE["started_at"] = now
        threading.Thread(target=_refresh_assembly_plan, args=(token,),
                         name="grit-wb-plan-refresh", daemon=True).start()
        if cached and cached.get("state") == "connected":
            return {**cached, "refreshing": True}
        return {"state": "syncing", "read_only": True,
                "details": "Получаем статусы заказов WB"}
