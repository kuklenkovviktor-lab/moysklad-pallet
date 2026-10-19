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
