"""PostgreSQL-only isolated routing reservation trial for #192.

Never import this into app.py. Explicit synthetic gate and isolated test
database name are mandatory. No WB API, printer I/O or production migrations.

The caller provides a *trusted* server-side validation callback receiving a
PostgreSQL connection inside the same transaction. In production, validating
real orders also requires locks on authoritative order / mapping rows and a
dedicated design review. This adapter is not a live picking endpoint.
"""
import os
from dataclasses import dataclass

import psycopg


@dataclass(frozen=True)
class PGReservationResult:
    accepted: bool
    reason: str
    reservation_id: str = ""
    printer_id: str = ""


SCHEMA = """
CREATE TABLE IF NOT EXISTS synthetic_line_printer_claims (
    account_id text NOT NULL,
    order_id text NOT NULL,
    request_id text NOT NULL UNIQUE,
    printer_id text NOT NULL,
    employee_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, order_id)
);
"""


def _verify_isolated_connection(conn):
    if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Synthetic PostgreSQL test gate is disabled")
    # Validate on the connection itself, not based on a supplied DSN string.
    row = conn.execute("SELECT current_database(), current_user").fetchone()
    db_name = str(row[0])
    if not db_name.startswith("grit_pr192_synthetic_"):
        raise RuntimeError("Refusing non-synthetic PostgreSQL database")
    return db_name


def initialize_synthetic_pg(conn):
    _verify_isolated_connection(conn)
    with conn.transaction():
        conn.execute(SCHEMA)


def reserve_synthetic_pg(
    connection_factory, *,
    account_id, order_id, printer_id, employee_id, request_id,
    is_eligible_locked,
):
    """Serialize competing claims on a synthetic order with an advisory lock.

    Only transaction-scoped lock, no print or WB mutation. Unique(account,order)
    in the DB remains the final defense if lock coverage is imperfect.
    """
    identifiers = (account_id, order_id, printer_id, employee_id, request_id)
    if any(not isinstance(x, str) or not x.strip() or len(x) > 160 for x in identifiers):
        return PGReservationResult(False, "INVALID_INPUT")
    if not callable(is_eligible_locked):
        return PGReservationResult(False, "ELIGIBILITY_REQUIRED")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            # Serializes the same account/order even before any row exists.
            # Hash collision only causes *extra* serialization, never duplicate
            # claims. UNIQUE constraints still determine correctness.
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                (account_id + "\x1f" + order_id,))
            row = conn.execute("""
                SELECT request_id, printer_id, employee_id
                FROM synthetic_line_printer_claims
                WHERE account_id=%s AND order_id=%s
                FOR UPDATE
            """, (account_id, order_id)).fetchone()
            if row:
                if row == (request_id, printer_id, employee_id):
                    return PGReservationResult(
                        True, "ALREADY_RESERVED_SAME_REQUEST", row[0], row[1])
                return PGReservationResult(
                    False, "ORDER_RESERVED_ELSEWHERE", row[0], row[1])
            if conn.execute("""
                SELECT 1 FROM synthetic_line_printer_claims
                WHERE request_id=%s
            """, (request_id,)).fetchone():
                return PGReservationResult(False, "REQUEST_ID_REUSED")
            if is_eligible_locked(conn) is not True:
                return PGReservationResult(False, "ORDER_INELIGIBLE")
            inserted = conn.execute("""
                INSERT INTO synthetic_line_printer_claims
                    (account_id,order_id,request_id,printer_id,employee_id)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
                RETURNING request_id
            """, (account_id, order_id, request_id, printer_id, employee_id)).fetchone()
            if not inserted:
                return PGReservationResult(False, "CONCURRENT_CONFLICT")
            return PGReservationResult(True, "RESERVED", request_id, printer_id)
