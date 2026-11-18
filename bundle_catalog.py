"""Read-only MoySklad bundle snapshot for the existing GRIT PostgreSQL catalog.

Bundles are independent assortment items, not four unit-product scans.
Never confuse MoySklad 'code' with its separate 'barcodes' field.
"""
import json
from datetime import datetime, timezone

from wms import db_connect, now_iso, query_all, query_one, sql


def init_bundle_catalog_db():
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fbs_catalog_bundles (
                bundle_id TEXT PRIMARY KEY,
                article TEXT NOT NULL,
                name TEXT NOT NULL,
                code TEXT NOT NULL,
                barcodes_json TEXT NOT NULL,
                synced_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fbs_catalog_meta (
                name TEXT PRIMARY KEY, value TEXT NOT NULL
            )
        """)
        conn.commit()
        cur.close()


def bundle_barcodes(entry):
    codes = set()
    for barcode in entry.get("barcodes") or []:
        if isinstance(barcode, str) and barcode.strip():
            codes.add(barcode.strip())
        elif isinstance(barcode, dict):
            for key in ("ean13", "ean8", "code128", "gtin", "upc", "barcode"):
                if barcode.get(key):
                    codes.add(str(barcode[key]).strip())
    return sorted(codes - {""})


def persist_bundle_snapshot(entries):
    """Atomically replace bundles only after complete successful retrieval."""
    if not isinstance(entries, list):
        raise ValueError("Incomplete bundle snapshot")
    now = now_iso()
    records = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("archived"):
            continue
        bundle_id = str(entry.get("id") or "").strip()
        if not bundle_id:
            raise ValueError("Bundle without an ID in the snapshot")
        records[bundle_id] = (
            bundle_id, str(entry.get("article") or "").strip(),
            str(entry.get("name") or "").strip(), str(entry.get("code") or "").strip(),
            json.dumps(bundle_barcodes(entry), ensure_ascii=False), now
        )
    # An empty snapshot is permitted only for a confirmed empty API collection.
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM fbs_catalog_bundles")
        if records:
            cur.executemany(sql("""
                INSERT INTO fbs_catalog_bundles
                (bundle_id, article, name, code, barcodes_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """), list(records.values()))
        cur.execute(sql("""
            INSERT INTO fbs_catalog_meta(name,value) VALUES (?,?)
            ON CONFLICT(name) DO UPDATE SET value=excluded.value
        """), ("bundles_last_successful_sync", now))
        conn.commit()
        cur.close()
    return len(records)


def refresh_bundle_catalog():
    """Bounded, read-only GET pagination; no partial snapshot on API error."""
    import os
    from ozon_fbo_catalog_validation import API_BASE, CatalogUnavailable, _catalog_get

    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    if not token:
        raise CatalogUnavailable("Не настроен токен МойСклад для комплектов.")
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json;charset=utf-8"}
    entries, size = [], 100
    for page in range(100):
        response = _catalog_get(
            API_BASE + "/entity/bundle", headers=headers,
            params={"limit": size, "offset": page * size}, timeout=15)
        if not response.ok:
            raise CatalogUnavailable(
                "МойСклад: ошибка загрузки комплектов HTTP " + str(response.status_code))
        try:
            payload = response.json()
        except ValueError as exc:
            raise CatalogUnavailable("МойСклад вернул некорректный список комплектов.") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise CatalogUnavailable("МойСклад вернул неожиданный формат комплектов.")
        rows = payload["rows"]
        entries.extend(rows)
        if len(rows) < size:
            return persist_bundle_snapshot(entries)
    raise CatalogUnavailable("Каталог комплектов превышает 10000 записей; снимок не обновлён.")


def recent_bundles(max_age_hours=48):
    try:
        stamp_row = query_one(sql(
            "SELECT value FROM fbs_catalog_meta WHERE name=?"
        ), ("bundles_last_successful_sync",))
        if not stamp_row:
            return []
        stamp = datetime.fromisoformat(str(stamp_row[0]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return []
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age < -300 or age > max_age_hours * 3600:
            return []
        rows = query_all(
            "SELECT bundle_id,article,name,code,barcodes_json FROM fbs_catalog_bundles"
        )
        return [{
            "id": str(row[0]), "article": str(row[1]), "name": str(row[2]),
            "code": str(row[3]), "barcodes": json.loads(row[4]),
            "entity_type": "bundle",
        } for row in rows]
    except (ValueError, TypeError, OverflowError):
        return []
    except Exception:
        return []
