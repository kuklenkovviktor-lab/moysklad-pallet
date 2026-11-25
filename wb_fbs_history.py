"""Persistent WB FBS observations and auditable daily throughput.

Only records state transitions *observed after activation*. Never invent a
historical shipment, and never equate an old delivery status with today's work.
The FBS order is one merchandise unit under the configured WB business rule.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os

from wms import db_connect, sql, query_all

MOSCOW = ZoneInfo("Europe/Moscow")

_CREATE = """CREATE TABLE IF NOT EXISTS wb_fbs_status_tracking (
  order_id TEXT PRIMARY KEY,
  entered_at TEXT NOT NULL,
  supplier_status TEXT NOT NULL,
  delivered_at TEXT,
  last_seen_at TEXT NOT NULL
)"""
_INDEX = "CREATE INDEX IF NOT EXISTS idx_wb_fbs_status_entered ON wb_fbs_status_tracking(entered_at)"
_INDEX_DELIVERY = "CREATE INDEX IF NOT EXISTS idx_wb_fbs_status_delivered ON wb_fbs_status_tracking(delivered_at)"


def record_observed_statuses(statuses, observed_at=None):
    """Commit a complete verified WB API snapshot, without storing new orders.

    New rows are registered only on first observed 'confirm'. Only previously
    tracked orders can later count as 'deliver'. Repeated snapshots are idempotent.
    """
    if not isinstance(statuses, dict):
        raise ValueError("Неверный снимок статусов WB")
    instant = observed_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not isinstance(instant, str):
        raise ValueError("Неверное время WB")
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(_CREATE)
            cur.execute(_INDEX)
            cur.execute(_INDEX_DELIVERY)
            cur.execute("SELECT order_id,supplier_status,delivered_at FROM wb_fbs_status_tracking")
            existing = {str(order_id): (status, delivered_at)
                        for order_id, status, delivered_at in cur.fetchall()}
            started = 0
            delivered = 0
            for ident, status in statuses.items():
                if type(ident) is not int or not isinstance(status, str):
                    continue
                order_id = str(ident)
                if order_id not in existing:
                    if status != "confirm":
                        continue
                    cur.execute(sql("INSERT INTO wb_fbs_status_tracking "
                                    "(order_id,entered_at,supplier_status,delivered_at,last_seen_at) "
                                    "VALUES (?,?,?,?,?)"),
                                (order_id, instant, "confirm", None, instant))
                    existing[order_id] = ("confirm", None)
                    started += 1
                    continue
                previous, delivered_at = existing[order_id]
                if previous == status:
                    continue  # avoid rewriting thousands of unchanged rows on each dashboard poll
                new_delivered = instant if previous != "deliver" and status == "deliver" and not delivered_at else delivered_at
                if new_delivered != delivered_at:
                    delivered += 1
                cur.execute(sql("UPDATE wb_fbs_status_tracking "
                                "SET supplier_status=?,delivered_at=?,last_seen_at=? WHERE order_id=?"),
                            (status, new_delivered, instant, order_id))
                existing[order_id] = (status, new_delivered)
            conn.commit()
            return {"first_seen_assembly": started, "first_seen_delivery": delivered}
        finally:
            cur.close()


def _bounds(date_key):
    try:
        dt = datetime.strptime(date_key, "%Y-%m-%d").replace(tzinfo=MOSCOW)
    except (ValueError, TypeError) as exc:
        raise ValueError("Дата должна быть YYYY-MM-DD") from exc
    today = datetime.now(MOSCOW).date()
    if dt.date() > today or dt.date() < today - timedelta(days=366):
        raise ValueError("Дата вне допустимого периода")
    start = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    end = (dt + timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
    return start, end


def daily_history(date_key):
    """Separate independently verifiable metrics: observed inflow, scans, WB deliver."""
    start, end = _bounds(date_key)
    unknown_admission = 0
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(_CREATE)
            cur.execute(_INDEX)
            cur.execute(_INDEX_DELIVERY)
            cur.execute(sql(
                "SELECT COUNT(*) FROM wb_fbs_status_tracking "
                "WHERE entered_at >= ? AND entered_at < ?"), (start, end))
            entered = int(cur.fetchone()[0] or 0)
            cur.execute(sql(
                "SELECT COUNT(*) FROM wb_fbs_status_tracking "
                "WHERE delivered_at >= ? AND delivered_at < ?"), (start, end))
            shipped = int(cur.fetchone()[0] or 0)
            if os.getenv("WB_FBS_V11_ENABLED", "0") == "1":
                account = os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"
                from wb_unpicked_manager import _assembly_day, UNKNOWN_DAY
                cur.execute(sql("""SELECT q.accepted_at FROM wb_fbs_orders o
                    LEFT JOIN wb_fbs_picking_queue q
                    ON q.account_id=o.account_id AND q.order_id=o.order_id
                    WHERE o.account_id=?"""), (account,))
                days = [_assembly_day(row[0]) for row in cur.fetchall()]
                entered = days.count(date_key)
                unknown_admission = days.count(UNKNOWN_DAY)
                cur.execute(sql("""SELECT observed_at FROM wb_fbs_delivery_observations
                    WHERE account_id=?"""), (account,))
                shipped = sum(_assembly_day(row[0]) == date_key for row in cur.fetchall())
            cur.execute(sql(
                "SELECT COUNT(*) FROM scans "
                "WHERE scanned_at >= ? AND scanned_at < ?"), (start, end))
            picked = int(cur.fetchone()[0] or 0)
            cur.execute(sql(
                "SELECT e.name,COUNT(*) FROM scans s "
                "JOIN employees e ON e.id=s.employee_id "
                "WHERE s.scanned_at >= ? AND s.scanned_at < ? "
                "GROUP BY s.employee_id,e.name ORDER BY COUNT(*) DESC"),(start,end))
            workers = [{"name":r[0],"picked":int(r[1])} for r in cur.fetchall()]
            conn.commit()
        finally:
            cur.close()
    return {"date":date_key, "new_on_assembly_observed":entered,
            "unknown_admission_orders":unknown_admission,
            "picked_in_grit":picked, "moved_to_delivery_observed":shipped,
            "workers":workers, "time_zone":"Europe/Moscow",
            "tracking_note":"WB статусы фиксируются при опросах; история начинается с включения наблюдения. "
                            "Доставка: наблюдаемый переход confirm → complete в API v3 (deliver в старом источнике), не приёмка на ПВЗ."}


def recent_history(days=7, today=None):
    date = today or datetime.now(MOSCOW).date()
    return [daily_history((date-timedelta(days=offset)).isoformat())
            for offset in range(min(31,max(1,int(days))))]
