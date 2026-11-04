"""Dedicated, durable metadata-only journal for GRIT marketplace HTTP sync attempts.

No credentials, request/response bodies, customer data or warehouse order writes.
Schema is created on manager's explicit visit to the HTTP lab, not at startup.
"""
from datetime import datetime, timezone
import uuid

from wms import db_connect, sql


def ensure_sync_journal():
    with db_connect() as db:
        db.cursor().execute("""CREATE TABLE IF NOT EXISTS grit_marketplace_sync_log (
            id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            platform TEXT NOT NULL,
            seller_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            outcome TEXT NOT NULL,
            attempts INTEGER NOT NULL,
            order_count INTEGER NOT NULL,
            http_status INTEGER,
            error_code TEXT NOT NULL,
            wait_seconds REAL NOT NULL
        )""")
        db.commit()


class PersistentJournal:
    def record(self, platform, seller, mode, status, attempts, count=0,
               http_status=None, error="", delay=0):
        ensure_sync_journal()
        entry = {
            "id": uuid.uuid4().hex,
            "at": datetime.now(timezone.utc).isoformat(),
            "platform": platform, "seller": seller, "mode": mode,
            "status": status, "attempts": int(attempts),
            "orders": int(count), "http_status": http_status,
            "error_code": str(error)[:80], "wait_seconds": round(float(delay), 2),
        }
        with db_connect() as db:
            db.cursor().execute(sql("""INSERT INTO grit_marketplace_sync_log
                (id,created_at,platform,seller_id,mode,outcome,attempts,order_count,
                 http_status,error_code,wait_seconds) VALUES (?,?,?,?,?,?,?,?,?,?,?)"""),
                (entry["id"], entry["at"], entry["platform"], entry["seller"],
                 entry["mode"], entry["status"], entry["attempts"], entry["orders"],
                 entry["http_status"], entry["error_code"], entry["wait_seconds"]))
            db.commit()
        return entry

    def recent(self, limit=40):
        ensure_sync_journal()
        limit = max(1, min(100, int(limit)))
        with db_connect() as db:
            rows = db.cursor().execute(sql("""SELECT id,created_at,platform,seller_id,
                mode,outcome,attempts,order_count,http_status,error_code,wait_seconds
                FROM grit_marketplace_sync_log ORDER BY created_at DESC LIMIT ?"""),
                (limit,)).fetchall()
        return [
            dict(zip(("id", "at", "platform", "seller", "mode", "status",
                      "attempts", "orders", "http_status", "error_code",
                      "wait_seconds"), row))
            for row in rows
        ]
