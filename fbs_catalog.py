"""Persistent, FBS-only barcode snapshot; receiving still uses its existing rules.

One atomic snapshot refresh keeps the previous catalog usable during MoySklad
network errors. Product barcodes and packaging barcodes remain distinct so FBS
cannot silently accept a multi-unit pallet barcode.
"""
import math

from wms import db_connect, now_iso, query_one, sql


def init_fbs_catalog_db():
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fbs_catalog_barcodes (
                barcode TEXT PRIMARY KEY,
                product_id TEXT NOT NULL DEFAULT '',
                article TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                quantity REAL NOT NULL,
                is_pack INTEGER NOT NULL DEFAULT 0,
                synced_at TEXT NOT NULL
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS fbs_catalog_meta (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()
        cur.close()
    # Shared PostgreSQL catalog keeps bundles separately typed to protect FBS scans.
    from bundle_catalog import init_bundle_catalog_db
    init_bundle_catalog_db()


def _product_fields(product):
    if not isinstance(product, dict):
        return None
    return (
        str(product.get("id") or ""),
        str(product.get("article") or ""),
        str(product.get("name") or ""),
    )


def save_fbs_catalog_snapshot(unit_barcodes, pack_barcodes):
    """Replace only after a complete successful warm-up.

    The same barcode can appear in both indexes; packaging takes precedence,
    matching the existing live-memory lookup and keeping the quantity safeguard.
    """
    stamp = now_iso()
    rows_by_barcode = {}
    for barcode, product in unit_barcodes.items():
        code = str(barcode or "").strip()
        fields = _product_fields(product)
        if code and fields:
            rows_by_barcode[code] = (code, *fields, 1.0, 0, stamp)
    for barcode, hit in pack_barcodes.items():
        code = str(barcode or "").strip()
        if not code or not isinstance(hit, dict):
            continue
        fields = _product_fields(hit.get("product"))
        pack = hit.get("pack") or {}
        try:
            quantity = float(pack.get("quantity") or 1)
        except (ValueError, TypeError):
            continue
        if fields and math.isfinite(quantity) and quantity > 0:
            rows_by_barcode[code] = (code, *fields, quantity, 1, stamp)

    if not rows_by_barcode:
        raise ValueError("Refusing to replace the FBS catalog with an empty snapshot")

    # All changes commit together. On an error, the old full snapshot survives.
    with db_connect() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM fbs_catalog_barcodes")
        cur.executemany(sql("""
            INSERT INTO fbs_catalog_barcodes
            (barcode, product_id, article, name, quantity, is_pack, synced_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """), list(rows_by_barcode.values()))
        cur.execute(sql("""
            INSERT INTO fbs_catalog_meta(name, value) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value
        """), ("last_successful_sync", stamp))
        conn.commit()
        cur.close()
    return len(rows_by_barcode)


def find_persisted_fbs_barcode(barcode):
    # Query through the shared SQL adapter for SQLite test and production PG.
    row = query_one(sql(
        "SELECT product_id, article, name, quantity, is_pack "
        "FROM fbs_catalog_barcodes WHERE barcode = ?"
    ), (str(barcode).strip(),))
    if not row:
        return None, None
    product = {"id": row[0], "article": row[1], "name": row[2]}
    pack = {"quantity": float(row[3]), "name": "Упаковка" if row[4] else "ШК товара"}
    return product, pack


def fbs_catalog_status():
    try:
        count = query_one("SELECT COUNT(*) FROM fbs_catalog_barcodes")
        last = query_one(
            sql("SELECT value FROM fbs_catalog_meta WHERE name = ?"),
            ("last_successful_sync",),
        )
        return {
            "persisted_barcodes": int(count[0] if count else 0),
            "last_successful_sync": str(last[0]) if last else None,
        }
    except Exception:
        return {"persisted_barcodes": 0, "last_successful_sync": None}


def ozon_catalog_snapshot(max_age_hours=48):
    """Read a complete-enough, recent FBS barcode snapshot for Ozon preflight.

    Returns None when absent/stale. No network calls and no writes.
    The FBS barcode index has precedence for packaging barcodes: never
    silently interpret a packaging code as a unit-product barcode.
    """
    from datetime import datetime, timezone
    from wms import query_all

    try:
        stamp_row = query_one(sql(
            "SELECT value FROM fbs_catalog_meta WHERE name = ?"
        ), ("last_successful_sync",))
        if not stamp_row:
            return None
        stamp = datetime.fromisoformat(str(stamp_row[0]).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return None
        age = (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds()
        if age < -300 or age > max_age_hours * 3600:
            return None

        rows = query_all(
            "SELECT barcode, product_id, article, name, is_pack "
            "FROM fbs_catalog_barcodes"
        )
        if not rows:
            return None

        products = {}
        for code, product_id, article, name, is_pack in rows:
            article = str(article or "").strip()
            product_id = str(product_id or "").strip()
            if not article or not product_id:
                continue
            by_id = products.setdefault(article.casefold(), {})
            record = by_id.setdefault(product_id, {
                "article": article, "name": str(name or ""), "barcodes": set()
            })
            if not bool(is_pack):
                record["barcodes"].add(str(code).strip())
        # Include independently typed bundle cards from the same database.
        # Missing bundle sync does not invalidate the existing FBS product snapshot.
        from bundle_catalog import recent_bundles
        for bundle in recent_bundles(max_age_hours):
            article = bundle["article"].strip()
            if not article:
                continue
            products.setdefault(article.casefold(), {})["bundle:" + bundle["id"]] = {
                "article": article, "name": bundle["name"],
                "code": bundle["code"], "entity_type": "bundle",
                "barcodes": set(bundle["barcodes"]),
            }
        return products or None
    except (ValueError, TypeError, OverflowError):
        return None
    except Exception:
        # Missing table or unavailable DB must never be interpreted as a match.
        return None


def search_ozon_catalog_snapshot(query, limit=30):
    """Search only fresh local unit products; no fallback API reads."""
    snapshot = ozon_catalog_snapshot()
    if snapshot is None:
        return None
    needle = str(query or "").strip().casefold()
    found = []
    for entries in snapshot.values():
        for product_id, product in entries.items():
            barcodes = sorted(product["barcodes"])
            if not barcodes:
                continue
            if needle in " ".join((product["article"], product["name"], " ".join(barcodes))).casefold():
                found.append({
                    "id": product_id.removeprefix("bundle:") if product_id.startswith("bundle:") else product_id,
                    "entity_type": product.get("entity_type", "product"),
                    "name": product["name"], "article": product["article"],
                    "code": product.get("code", ""), "barcodes": barcodes,
                })
                if len(found) >= limit:
                    return found
    return found
