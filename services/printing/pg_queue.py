"""Isolated PostgreSQL 17 print-job queue contract, NOT a production adapter.

Never imported by app.py. No HTTP endpoints, WB API, USB printing, or live
schema changes. Eligibility and authorization callbacks must be trusted
server-side functions validating current state WITHIN the same PG transaction.
"""
from dataclasses import dataclass

from line_printer_pg_trial import _verify_isolated_connection


DDL = (
    """CREATE TABLE IF NOT EXISTS synthetic_pr192_print_jobs (
         job_id text PRIMARY KEY,
         account_id text NOT NULL,
         order_id text NOT NULL,
         sticker_kind text NOT NULL,
         printer_id text NOT NULL,
         state text NOT NULL CHECK(state IN
           ('QUEUED','SENDING','SUBMITTED','PRINTED','FAILED','UNKNOWN','CANCELLED')),
         version bigint NOT NULL DEFAULT 1,
         created_at timestamptz NOT NULL DEFAULT now(),
         updated_at timestamptz NOT NULL DEFAULT now(),
         UNIQUE(account_id,order_id,sticker_kind)
       )""",
    """CREATE TABLE IF NOT EXISTS synthetic_pr192_print_events (
         event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
         action_id text NOT NULL UNIQUE,
         job_id text NOT NULL REFERENCES synthetic_pr192_print_jobs(job_id),
         actor_id text NOT NULL,
         event text NOT NULL,
         from_state text,
         to_state text NOT NULL,
         reason text NOT NULL DEFAULT '',
         created_at timestamptz NOT NULL DEFAULT now()
       )""",
    """CREATE INDEX IF NOT EXISTS synthetic_pr192_print_pending_idx
       ON synthetic_pr192_print_jobs(printer_id,state)""",
)


@dataclass(frozen=True)
class EnqueueResult:
    accepted: bool
    reason: str
    added: int = 0
    existing: int = 0
    elsewhere: int = 0
    review: int = 0


@dataclass(frozen=True)
class PrintTransition:
    accepted: bool
    reason: str
    state: str


def _valid(*items):
    return all(isinstance(x, str) and bool(x.strip()) and len(x) <= 160
               for x in items)


def initialize_synthetic_print_pg(conn):
    _verify_isolated_connection(conn)
    with conn.transaction():
        for sql in DDL:
            conn.execute(sql)


def enqueue_synthetic_print_pg(connection_factory, *, printer_id, actor_id,
                               candidates, authorized_locked, eligible_locked):
    """Atomic batch of (account_id, order_id, sticker_kind) tuples.

    CANDIDATES ARE NOT SAFE CLIENT INPUT. The caller must derive the batch
    from current server-side WB queue, and eligible_locked MUST validate the
    original sticker, SKU/cells, QR, actor, account and status under DB locks.
    No print job is dispatched here. Sorting lock keys avoids deadlocks.
    """
    if not _valid(printer_id, actor_id) or not isinstance(candidates, (list, tuple)):
        return EnqueueResult(False, 'INVALID_INPUT')
    if not callable(authorized_locked) or not callable(eligible_locked):
        return EnqueueResult(False, 'TRUSTED_VALIDATION_REQUIRED')
    try:
        keys = sorted(set(tuple(candidate) for candidate in candidates))
    except (TypeError, ValueError):
        return EnqueueResult(False, 'INVALID_CANDIDATES')
    if any(not isinstance(key, tuple) or len(key) != 3 or not _valid(*key)
           for key in keys):
        return EnqueueResult(False, 'INVALID_CANDIDATES')

    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            if authorized_locked(conn, printer_id, actor_id) is not True:
                return EnqueueResult(False, 'AUTHORIZATION_DENIED')
            added = existing = elsewhere = review = 0
            for account_id, order_id, kind in keys:
                # Serialize competing scans on an order before a row exists.
                lock_key = '\x1f'.join((account_id, order_id, kind))
                conn.execute(
                    'SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                    ('print-job:' + lock_key,))
                row = conn.execute(
                    """SELECT job_id,printer_id,state FROM synthetic_pr192_print_jobs
                       WHERE account_id=%s AND order_id=%s AND sticker_kind=%s
                       FOR UPDATE""", (account_id, order_id, kind)).fetchone()
                if row:
                    if row[1] != printer_id:
                        elsewhere += 1
                    elif row[2] in ('UNKNOWN', 'FAILED'):
                        review += 1
                    else:
                        existing += 1
                    continue
                # Revalidate only inside the same locked transaction.
                if eligible_locked(conn, account_id, order_id, kind, printer_id) is not True:
                    review += 1
                    continue
                import uuid
                job_id = uuid.uuid4().hex
                inserted = conn.execute(
                    """INSERT INTO synthetic_pr192_print_jobs
                       (job_id,account_id,order_id,sticker_kind,printer_id,state)
                       VALUES (%s,%s,%s,%s,%s,'QUEUED') ON CONFLICT DO NOTHING
                       RETURNING job_id""",
                    (job_id, account_id, order_id, kind, printer_id)).fetchone()
                if not inserted:
                    elsewhere += 1
                    continue
                conn.execute(
                    """INSERT INTO synthetic_pr192_print_events
                       (action_id,job_id,actor_id,event,from_state,to_state,reason)
                       VALUES (%s,%s,%s,'ENQUEUE',NULL,'QUEUED','')""",
                    (uuid.uuid4().hex, job_id, actor_id))
                added += 1
            return EnqueueResult(True, 'QUEUE_UPDATED', added, existing, elsewhere, review)


_ALLOWED = {
    'QUEUED': frozenset({'SENDING', 'CANCELLED'}),
    'SENDING': frozenset({'SUBMITTED', 'FAILED', 'UNKNOWN'}),
    'SUBMITTED': frozenset({'PRINTED', 'UNKNOWN'}),
    'FAILED': frozenset({'QUEUED', 'CANCELLED'}),
    'UNKNOWN': frozenset({'QUEUED', 'CANCELLED'}),
    'PRINTED': frozenset(),
    'CANCELLED': frozenset(),
}


def transition_synthetic_print_pg(connection_factory, *, job_id, action_id,
                                  actor_id, target, authorized_locked,
                                  reason='', evidence=''):
    """Change one job inside a transaction. No physical evidence is inferred.

    authorized_locked(conn, row, target) is a required, trusted callback.
    PHYSICAL_VERIFIED and REVIEWED_SAFE_TO_RETRY are synthetic evidence tokens,
    NOT proof that a real Xprinter printed anything.
    """
    if not _valid(job_id, action_id, actor_id, target):
        return PrintTransition(False, 'INVALID_INPUT', 'UNKNOWN')
    if target not in _ALLOWED or not callable(authorized_locked):
        return PrintTransition(False, 'INVALID_TARGET_OR_AUTH', 'UNKNOWN')
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        with conn.transaction():
            # Consistent lock order: action ID, then job ID.
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                         ('print-action:' + action_id,))
            prior = conn.execute(
                """SELECT job_id,to_state FROM synthetic_pr192_print_events
                   WHERE action_id=%s""", (action_id,)).fetchone()
            if prior:
                if prior != (job_id, target):
                    return PrintTransition(False, 'ACTION_ID_REUSED', prior[1])
                return PrintTransition(False, 'ACTION_ALREADY_RECORDED', prior[1])
            row = conn.execute(
                """SELECT job_id,account_id,order_id,sticker_kind,printer_id,state
                   FROM synthetic_pr192_print_jobs WHERE job_id=%s FOR UPDATE""",
                (job_id,)).fetchone()
            if not row:
                return PrintTransition(False, 'NOT_FOUND', 'UNKNOWN')
            old = row[5]
            if target not in _ALLOWED[old]:
                return PrintTransition(False, 'INVALID_TRANSITION', old)
            if authorized_locked(conn, row, target) is not True:
                return PrintTransition(False, 'AUTHORIZATION_DENIED', old)
            if target == 'PRINTED' and evidence != 'PHYSICAL_VERIFIED':
                return PrintTransition(False, 'PHYSICAL_PROOF_REQUIRED', old)
            if target == 'FAILED' and evidence != 'CERTAIN_NOT_PRINTED':
                return PrintTransition(False, 'FAILURE_PROOF_REQUIRED', old)
            if old in ('UNKNOWN', 'FAILED') and target == 'QUEUED':
                if evidence != 'REVIEWED_SAFE_TO_RETRY' or len(reason.strip()) < 5:
                    return PrintTransition(False, 'MANAGER_REVIEW_REQUIRED', old)
            if target == 'CANCELLED' and len(reason.strip()) < 5:
                return PrintTransition(False, 'CANCELLATION_REASON_REQUIRED', old)
            conn.execute(
                """UPDATE synthetic_pr192_print_jobs
                   SET state=%s,version=version+1,updated_at=now()
                   WHERE job_id=%s""", (target, job_id))
            conn.execute(
                """INSERT INTO synthetic_pr192_print_events
                   (action_id,job_id,actor_id,event,from_state,to_state,reason)
                   VALUES (%s,%s,%s,'TRANSITION',%s,%s,%s)""",
                (action_id, job_id, actor_id, old, target, reason.strip()))
            return PrintTransition(True, 'TRANSITION_OK', target)


def read_synthetic_print_jobs(connection_factory, *, printer_id):
    if not _valid(printer_id):
        raise ValueError('Invalid printer id')
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
        return conn.execute(
            """SELECT job_id,account_id,order_id,sticker_kind,printer_id,state,version
               FROM synthetic_pr192_print_jobs WHERE printer_id=%s
               ORDER BY created_at,job_id""", (printer_id,)).fetchall()
