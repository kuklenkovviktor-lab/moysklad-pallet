"""Offline reference reservation protocol for printer routing (#192).

SQLite-backed SYNTHETIC concurrency pilot. No Flask integration or production
schema changes. Every call opens its own short transaction, with BEGIN IMMEDIATE
to serialize competing writers. Port to PostgreSQL separately using unique
(account_id, order_id) and INSERT ... ON CONFLICT / row locks.

The caller MUST check authenticated employee, printer QR, current order status,
and fresh line coverage within the authoritative transaction before any live
integration. This prototype cannot provide those production guarantees.
"""
from dataclasses import dataclass
import sqlite3
from typing import Callable

@dataclass(frozen=True)
class ReservationResult:
    accepted: bool
    reason: str
    reservation_id: str = ""
    printer_id: str = ""


def initialize_synthetic_schema(connection):
    connection.execute("""
        CREATE TABLE IF NOT EXISTS synthetic_printer_reservations (
            account_id TEXT NOT NULL,
            order_id TEXT NOT NULL,
            reservation_id TEXT NOT NULL UNIQUE,
            printer_id TEXT NOT NULL,
            employee_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (account_id, order_id)
        )
    """)
    connection.commit()


def reserve_synthetic(
    db_path: str, *,
    account_id: str,
    order_id: str,
    printer_id: str,
    employee_id: str,
    reservation_id: str,
    is_eligible: Callable[[], bool],
) -> ReservationResult:
    """Reserve exactly one order once; never move or silently replay an order.

    is_eligible is an isolated test callback. Production MUST re-read
    authoritative order and mapping state inside the DB transaction, not
    trust a callback or stale availability snapshot.
    """
    ids = (account_id, order_id, printer_id, employee_id, reservation_id)
    if any(not isinstance(value, str) or not value.strip() or len(value) > 160
           for value in ids):
        return ReservationResult(False, "INVALID_INPUT")
    if not callable(is_eligible):
        return ReservationResult(False, "ELIGIBILITY_REQUIRED")
    # Existing reservation_id must not be accepted for any other order either.
    with sqlite3.connect(db_path, timeout=10, isolation_level=None) as db:
        db.execute("PRAGMA busy_timeout=10000")
        try:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("""
                SELECT reservation_id, printer_id, employee_id
                FROM synthetic_printer_reservations
                WHERE account_id=? AND order_id=?
            """, (account_id, order_id)).fetchone()
            if existing is not None:
                db.execute("ROLLBACK")
                if (existing[0] == reservation_id and existing[1] == printer_id
                        and existing[2] == employee_id):
                    return ReservationResult(True, "ALREADY_RESERVED_SAME_REQUEST",
                                             existing[0], existing[1])
                return ReservationResult(False, "ORDER_RESERVED_ELSEWHERE",
                                         existing[0], existing[1])
            collision = db.execute("""
                SELECT 1 FROM synthetic_printer_reservations WHERE reservation_id=?
            """, (reservation_id,)).fetchone()
            if collision is not None:
                db.execute("ROLLBACK")
                return ReservationResult(False, "REQUEST_ID_REUSED")
            if is_eligible() is not True:
                db.execute("ROLLBACK")
                return ReservationResult(False, "ORDER_INELIGIBLE")
            db.execute("""
                INSERT INTO synthetic_printer_reservations
                    (account_id,order_id,reservation_id,printer_id,employee_id)
                VALUES (?,?,?,?,?)
            """, (account_id, order_id, reservation_id, printer_id, employee_id))
            db.execute("COMMIT")
            return ReservationResult(True, "RESERVED", reservation_id, printer_id)
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise


def get_synthetic_reservation(db_path: str, account_id: str, order_id: str):
    with sqlite3.connect(db_path) as db:
        row = db.execute("""
            SELECT reservation_id, printer_id, employee_id
            FROM synthetic_printer_reservations
            WHERE account_id=? AND order_id=?
        """, (account_id, order_id)).fetchone()
    return row
