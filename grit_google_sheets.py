"""Read-only Google Sheets catalogue via a least-privilege service account.

Configuration lives in HOSTKEY environment, never repository/DB. The server must
be granted Viewer access to the specific spreadsheet. No public sharing needed.
Calls occur on manager preview or status; a 15-min in-process cache bounds calls.
No warehouse operations or background jobs are started.
"""
import json
import os
import threading
import time

import requests

from ozon_fbo_dimensions import read_dimensions_values

_CACHE = {"until": 0, "catalog": None, "synced_at": None, "spreadsheet_id": None}
_LOCK = threading.Lock()
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_TTL_SECONDS = 900


class GoogleSheetsSyncError(Exception):
    pass


def sync_configuration():
    account = os.getenv("GRIT_GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    key_file = os.getenv("GRIT_GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    account_available = bool(account or (key_file and os.path.isfile(key_file)))
    spreadsheet_id = os.getenv("GRIT_GOOGLE_SPREADSHEET_ID", "").strip()
    return {"configured": bool(account_available and spreadsheet_id),
            "spreadsheet_id": spreadsheet_id,
            "service_account_present": account_available}


def _safe_settings():
    cfg = sync_configuration()
    if not cfg["configured"]:
        raise GoogleSheetsSyncError("Подключение Google Sheets ещё не настроено на сервере")
    sid = cfg["spreadsheet_id"]
    if not sid or len(sid) > 160 or not all(ch.isalnum() or ch in "-_" for ch in sid):
        raise GoogleSheetsSyncError("Неверный идентификатор таблицы")
    try:
        key_file = os.getenv("GRIT_GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
        if key_file:
            with open(key_file, "r", encoding="utf-8") as stream:
                info = json.load(stream)
        else:
            info = json.loads(os.environ["GRIT_GOOGLE_SERVICE_ACCOUNT_JSON"])
        if not isinstance(info, dict) or info.get("type") != "service_account":
            raise ValueError("service account expected")
        if not info.get("client_email") or not info.get("private_key"):
            raise ValueError("credentials incomplete")
    except (ValueError, TypeError, KeyError, OSError):
        raise GoogleSheetsSyncError("Учётные данные Google Sheets настроены неверно")
    return info, sid


def _google_values(info, spreadsheet_id):
    # OAuth token generated server-side; never returned to the browser or logged.
    try:
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[SHEETS_SCOPE])
        credentials.refresh(Request())
        url = "https://sheets.googleapis.com/v4/spreadsheets/" + spreadsheet_id + "/values/\u0027Товары\u0027!A1:D3000"
        response = requests.get(
            url, headers={"Authorization": "Bearer " + credentials.token,
                          "Accept": "application/json"},
            params={"valueRenderOption": "FORMATTED_VALUE"},
            timeout=12, allow_redirects=False,
        )
    except Exception as exc:
        # Never include upstream messages (may contain tokens or sheet information).
        raise GoogleSheetsSyncError("Google Sheets недоступен для чтения") from exc
    if response.status_code in (401, 403):
        raise GoogleSheetsSyncError("Google Sheets отказал в доступе. Дай сервисному аккаунту роль Читатель")
    if response.status_code == 404:
        raise GoogleSheetsSyncError("Таблица Google Sheets не найдена для сервисного аккаунта")
    if response.status_code != 200:
        raise GoogleSheetsSyncError("Google Sheets вернул HTTP " + str(response.status_code))
    try:
        rows = response.json().get("values", [])
        if not isinstance(rows, list):
            raise ValueError("unexpected rows")
        return rows
    except (TypeError, ValueError, AttributeError, IndexError) as exc:
        raise GoogleSheetsSyncError("Google Sheets вернул неожиданный формат данных") from exc


def get_google_catalog(force=False):
    info, sid = _safe_settings()
    now = time.monotonic()
    if not force and _CACHE["catalog"] is not None and _CACHE["spreadsheet_id"] == sid and now < _CACHE["until"]:
        return _CACHE["catalog"], _CACHE["synced_at"], True
    with _LOCK:
        now = time.monotonic()
        if not force and _CACHE["catalog"] is not None and _CACHE["spreadsheet_id"] == sid and now < _CACHE["until"]:
            return _CACHE["catalog"], _CACHE["synced_at"], True
        dimension_rows = _google_values(info, sid)
        try:
            catalog = read_dimensions_values(dimension_rows)
        except ValueError as exc:
            raise GoogleSheetsSyncError("Структура таблицы изменилась: " + str(exc)) from exc
        if not catalog["items"]:
            raise GoogleSheetsSyncError("В Google-таблице нет товаров с полными габаритами")
        from datetime import datetime, timezone
        synced_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _CACHE.update(catalog=catalog, synced_at=synced_at,
                      spreadsheet_id=sid, until=time.monotonic() + DEFAULT_TTL_SECONDS)
        return catalog, synced_at, False
