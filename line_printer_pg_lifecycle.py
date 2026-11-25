"""Synthetic-only PostgreSQL lifecycle for GRIT printer assignments (#192).

FREE -> RESERVED -> COMPLETED (terminal)
                -> RELEASED -> RESERVED (new request)

Only an explicitly authorized manager can release. Closing a shift, cancelling
WB orders or disabling a printer NEVER silently frees a reservation.
Every transition is an atomic DB transaction and writes an immutable audit row.
This module does not import app.py, change production schema or print anything.
"""
from dataclasses import dataclass
from line_printer_pg_trial import _verify_isolated_connection

STATES = frozenset({"FREE", "RESERVED", "COMPLETED", "RELEASED"})


@dataclass(frozen=True)
class Transition:
    accepted: bool
    reason: str
    state: str


DDL = (
    """CREATE TABLE IF NOT EXISTS synthetic_line_printer_lifecycle (
        account_id text NOT NULL,
        order_id text NOT NULL,
        state text NOT NULL CHECK(state IN ('FREE','RESERVED','COMPLETED','RELEASED')),
        reservation_id text,
        printer_id text,
        employee_id text,
        shift_id text,
        version bigint NOT NULL DEFAULT 0,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(account_id,order_id),
        CHECK ((state='RESERVED' AND reservation_id IS NOT NULL
                AND printer_id IS NOT NULL AND employee_id IS NOT NULL AND shift_id IS NOT NULL)
          OR state <> 'RESERVED')
    )""",
    """CREATE TABLE IF NOT EXISTS synthetic_line_printer_lifecycle_events (
        event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        action_id text NOT NULL UNIQUE,
        account_id text NOT NULL,
        order_id text NOT NULL,
        action text NOT NULL CHECK(action IN ('RESERVE','COMPLETE','RELEASE')),
        from_state text NOT NULL,
        to_state text NOT NULL,
        actor_id text NOT NULL,
        reason text NOT NULL,
        reservation_id text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE UNIQUE INDEX IF NOT EXISTS synthetic_lifecycle_reservation_unique
       ON synthetic_line_printer_lifecycle_events(reservation_id)
       WHERE action='RESERVE' AND reservation_id IS NOT NULL""",
)


def initialize_lifecycle(conn):
    _verify_isolated_connection(conn)
    with conn.transaction():
        for statement in DDL:
            conn.execute(statement)


def _valid(*values):
    return all(isinstance(v, str) and bool(v.strip()) and len(v) <= 160
               for v in values)


def _operate(connection_factory, *, account_id, order_id, action_id,
             action, actor_id, reservation_id="", printer_id="",
             employee_id="", shift_id="", reason="", authorized_locked=None):
    if not _valid(account_id, order_id, action_id, actor_id):
        return Transition(False, "INVALID_INPUT", "UNKNOWN")
    if action not in ("RESERVE", "COMPLETE", "RELEASE"):
        return Transition(False, "INVALID_ACTION", "UNKNOWN")
    if not callable(authorized_locked):
        return Transition(False, "AUTHORIZATION_REQUIRED", "UNKNOWN")
    if action == "RESERVE" and not _valid(reservation_id, printer_id, employee_id, shift_id):
        return Transition(False, "INVALID_INPUT", "UNKNOWN")
    if action in ("COMPLETE", "RELEASE") and not _valid(reservation_id):
        return Transition(False, "INVALID_INPUT", "UNKNOWN")
    if action == "RELEASE" and (not _valid(reason) or len(reason.strip()) < 5):
        return Transition(False, "RELEASE_REASON_REQUIRED", "UNKNOWN")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            # Lock before reading events: a concurrent replay cannot race.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                         (account_id + "\x1f" + order_id,))
            event = conn.execute("""
                SELECT account_id,order_id,action,actor_id,reservation_id,to_state
                FROM synthetic_line_printer_lifecycle_events WHERE action_id=%s
            """, (action_id,)).fetchone()
            if event:
                if event[:5] != (account_id, order_id, action, actor_id,
                                  reservation_id or None):
                    return Transition(False, "ACTION_ID_REUSED", event[5])
                # Replay is never a fresh authorization to work on an order.
                return Transition(False, "ACTION_ALREADY_RECORDED", event[5])

            row = conn.execute("""
                SELECT state,reservation_id,printer_id,employee_id,shift_id,version
                FROM synthetic_line_printer_lifecycle
                WHERE account_id=%s AND order_id=%s FOR UPDATE
            """, (account_id, order_id)).fetchone()
            old = row[0] if row else "FREE"
            if old == "COMPLETED":
                return Transition(False, "ALREADY_COMPLETED", old)
            if action == "RESERVE":
                if old == "RESERVED":
                    return Transition(False, "ALREADY_RESERVED", old)
                if old not in ("FREE", "RELEASED"):
                    return Transition(False, "INVALID_TRANSITION", old)
                new = "RESERVED"
            else:
                if old != "RESERVED":
                    return Transition(False, "NOT_RESERVED", old)
                if row[1] != reservation_id:
                    return Transition(False, "RESERVATION_MISMATCH", old)
                new = "COMPLETED" if action == "COMPLETE" else "RELEASED"
            # Callback is a trusted server-side check against live, locked
            # authorization and current order state. Caller must never accept
            # browser-supplied role flags or trust unverified QR payloads.
            if authorized_locked(conn, action, old, row) is not True:
                return Transition(False, "AUTHORIZATION_DENIED", old)

            if row:
                conn.execute("""
                    UPDATE synthetic_line_printer_lifecycle
                    SET state=%s, reservation_id=%s,printer_id=%s,
                        employee_id=%s,shift_id=%s,version=version+1,updated_at=now()
                    WHERE account_id=%s AND order_id=%s
                """, (new, reservation_id if action == "RESERVE" else row[1],
                      printer_id if action == "RESERVE" else row[2],
                      employee_id if action == "RESERVE" else row[3],
                      shift_id if action == "RESERVE" else row[4],
                      account_id, order_id))
            else:
                conn.execute("""
                    INSERT INTO synthetic_line_printer_lifecycle
                    (account_id,order_id,state,reservation_id,printer_id,
                     employee_id,shift_id,version)
                    VALUES (%s,%s,'RESERVED',%s,%s,%s,%s,1)
                """, (account_id, order_id, reservation_id, printer_id,
                      employee_id, shift_id))
            conn.execute("""
                INSERT INTO synthetic_line_printer_lifecycle_events
                (action_id,account_id,order_id,action,from_state,to_state,
                 actor_id,reason,reservation_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (action_id, account_id, order_id, action, old, new,
                  actor_id, reason.strip(), reservation_id))
            return Transition(True, action + "_OK", new)


def reserve_lifecycle(connection_factory, *, account_id, order_id, action_id,
                      actor_id, reservation_id, printer_id, employee_id,
                      shift_id, authorized_locked):
    return _operate(connection_factory, account_id=account_id, order_id=order_id,
                    action_id=action_id, action="RESERVE", actor_id=actor_id,
                    reservation_id=reservation_id, printer_id=printer_id,
                    employee_id=employee_id, shift_id=shift_id,
                    authorized_locked=authorized_locked)


def complete_lifecycle(connection_factory, *, account_id, order_id, action_id,
                       actor_id, reservation_id, authorized_locked):
    return _operate(connection_factory, account_id=account_id, order_id=order_id,
                    action_id=action_id, action="COMPLETE", actor_id=actor_id,
                    reservation_id=reservation_id, authorized_locked=authorized_locked)


def release_lifecycle(connection_factory, *, account_id, order_id, action_id,
                      actor_id, reservation_id, reason, authorized_locked):
    return _operate(connection_factory, account_id=account_id, order_id=order_id,
                    action_id=action_id, action="RELEASE", actor_id=actor_id,
                    reservation_id=reservation_id, reason=reason,
                    authorized_locked=authorized_locked)


def read_lifecycle(connection_factory, account_id, order_id):
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        row = conn.execute("""
            SELECT state,reservation_id,printer_id,employee_id,shift_id,version
            FROM synthetic_line_printer_lifecycle
            WHERE account_id=%s AND order_id=%s
        """, (account_id, order_id)).fetchone()
    return row if row else ("FREE", None, None, None, None, 0)
