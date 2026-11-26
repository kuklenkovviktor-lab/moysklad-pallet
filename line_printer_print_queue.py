"""Synthetic-only durable print queue for GRIT line printers.

This module does NOT authenticate users, read WB, connect to production,
open HTTP routes, use a spooler or prove physical output. Caller must pass
orders from a trusted, current, locked eligibility snapshot after QR/auth
verification. SQLite is a contract prototype, NOT production PostgreSQL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import sqlite3
import uuid

SCHEMA_VERSION = 1
ACTIVE = ('QUEUED', 'SENDING', 'SUBMITTED', 'UNKNOWN')
TERMINAL = ('PRINTED', 'CANCELLED')
STATES = ACTIVE + TERMINAL + ('FAILED',)


class PrintQueueError(ValueError):
    pass


@dataclass(frozen=True)
class EligibleOrder:
    account_id: str
    order_id: str
    sticker_kind: str = 'WB_FBS'


def _required(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise PrintQueueError('INVALID_' + name)
    return value.strip()


class SyntheticPrintQueue:
    """Isolated queue contract; SQLite locking is not a PostgreSQL proof."""

    def __init__(self, path=':memory:'):
        if os.getenv('GRIT_SYNTHETIC_PRINT_QUEUE') != 'YES_ISOLATED':
            raise RuntimeError('Synthetic print queue is disabled')
        if path != ':memory:' and not os.path.basename(path).startswith('grit_synthetic_print_'):
            raise RuntimeError('Refusing non-synthetic SQLite path')
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('PRAGMA busy_timeout=10000')
        self.conn.execute('PRAGMA foreign_keys=ON')
        if path != ':memory:':
            self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS print_jobs (
            id TEXT PRIMARY KEY, account_id TEXT NOT NULL, order_id TEXT NOT NULL,
            sticker_kind TEXT NOT NULL, printer_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN
              ('QUEUED','SENDING','SUBMITTED','PRINTED','FAILED','UNKNOWN','CANCELLED')),
            version INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(account_id, order_id, sticker_kind)
          );
          CREATE TABLE IF NOT EXISTS print_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL REFERENCES print_jobs(id),
            action_id TEXT NOT NULL UNIQUE, event TEXT NOT NULL, actor TEXT NOT NULL,
            from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL,
            created_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS print_jobs_printer_state ON print_jobs(printer_id,state);
        """)

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat()

    def _event(self, job_id, action_id, event, actor, old, new, reason=''):
        self.conn.execute("""INSERT INTO print_events
            (job_id, action_id, event, actor, from_state, to_state, reason, created_at)
            VALUES(?,?,?,?,?,?,?,?)""",
            (job_id, action_id, event, actor, old, new, reason, self._now()))

    def scan(self, *, printer_id: str, actor: str, eligible_orders: list[EligibleOrder],
             authorized: bool = False) -> dict:
        """Update queue with verified candidates; no retry/reprint/stealing."""
        if authorized is not True:
            raise PrintQueueError('AUTH_REQUIRED')
        printer_id = _required(printer_id, 'PRINTER')
        actor = _required(actor, 'ACTOR')
        if not isinstance(eligible_orders, (list, tuple)):
            raise PrintQueueError('INVALID_ORDERS')
        normalized = []
        for item in eligible_orders:
            if not isinstance(item, EligibleOrder):
                raise PrintQueueError('INVALID_ORDER_RECORD')
            normalized.append((_required(item.account_id, 'ACCOUNT'),
                               _required(item.order_id, 'ORDER'),
                               _required(item.sticker_kind, 'KIND')))
        counts = {'added': 0, 'already_queued': 0, 'already_handled': 0,
                  'owned_elsewhere': 0, 'requires_review': 0}
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            for account, order, kind in dict.fromkeys(normalized):
                row = self.conn.execute("""SELECT id,printer_id,state FROM print_jobs
                   WHERE account_id=? AND order_id=? AND sticker_kind=?""",
                   (account, order, kind)).fetchone()
                if row:
                    if row['printer_id'] != printer_id:
                        counts['owned_elsewhere'] += 1
                    elif row['state'] in ('UNKNOWN', 'FAILED'):
                        counts['requires_review'] += 1
                    elif row['state'] in TERMINAL:
                        counts['already_handled'] += 1
                    else:
                        counts['already_queued'] += 1
                    continue
                job_id = uuid.uuid4().hex
                now = self._now()
                self.conn.execute("""INSERT INTO print_jobs
                  (id,account_id,order_id,sticker_kind,printer_id,state,created_at,updated_at)
                  VALUES(?,?,?,?,?,'QUEUED',?,?)""",
                  (job_id, account, order, kind, printer_id, now, now))
                self._event(job_id, uuid.uuid4().hex, 'ENQUEUE', actor, None, 'QUEUED')
                counts['added'] += 1
            self.conn.execute('COMMIT')
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise
        return counts

    def transition(self, *, job_id: str, action_id: str, actor: str,
                   target: str, authorized: bool = False, reason: str = '',
                   evidence: str = '') -> str:
        """Only verified physical evidence can mark PRINTED; spool ACK cannot."""
        if authorized is not True:
            raise PrintQueueError('AUTH_REQUIRED')
        job_id = _required(job_id, 'JOB')
        action_id = _required(action_id, 'ACTION')
        actor = _required(actor, 'ACTOR')
        target = _required(target, 'TARGET')
        allowed = {
            'QUEUED': {'SENDING', 'CANCELLED'},
            'SENDING': {'SUBMITTED', 'UNKNOWN', 'FAILED'},
            'SUBMITTED': {'PRINTED', 'UNKNOWN'},
            'UNKNOWN': {'QUEUED', 'CANCELLED'},
            'FAILED': {'QUEUED', 'CANCELLED'},
            'PRINTED': set(), 'CANCELLED': set(),
        }
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            past = self.conn.execute('SELECT job_id,to_state FROM print_events WHERE action_id=?',
                                     (action_id,)).fetchone()
            if past:
                if past['job_id'] != job_id or past['to_state'] != target:
                    raise PrintQueueError('ACTION_ID_REUSED')
                self.conn.execute('COMMIT')
                return target
            row = self.conn.execute('SELECT state FROM print_jobs WHERE id=?', (job_id,)).fetchone()
            if row is None:
                raise PrintQueueError('JOB_NOT_FOUND')
            before = row['state']
            if target not in allowed[before]:
                raise PrintQueueError('INVALID_TRANSITION')
            if target == 'PRINTED' and evidence != 'PHYSICAL_VERIFIED':
                raise PrintQueueError('PHYSICAL_CONFIRMATION_REQUIRED')
            if before in ('UNKNOWN', 'FAILED') and target == 'QUEUED':
                if actor != 'manager' or len(reason.strip()) < 5 or evidence != 'REVIEWED_SAFE_TO_RETRY':
                    raise PrintQueueError('MANAGER_REVIEW_REQUIRED')
            if target == 'FAILED' and evidence != 'CERTAIN_NOT_PRINTED':
                raise PrintQueueError('CERTAIN_FAILURE_REQUIRED')
            if target == 'CANCELLED' and (not reason or actor != 'manager'):
                raise PrintQueueError('MANAGER_REASON_REQUIRED')
            self.conn.execute("""UPDATE print_jobs SET state=?,version=version+1,updated_at=?
                                 WHERE id=?""", (target, self._now(), job_id))
            self._event(job_id, action_id, 'TRANSITION', actor, before, target, reason)
            self.conn.execute('COMMIT')
            return target
        except BaseException:
            self.conn.execute('ROLLBACK')
            raise

    def jobs(self, printer_id=None):
        if printer_id is None:
            result = self.conn.execute('SELECT * FROM print_jobs ORDER BY created_at,id').fetchall()
        else:
            result = self.conn.execute('SELECT * FROM print_jobs WHERE printer_id=? ORDER BY created_at,id',
                                       (printer_id,)).fetchall()
        return [dict(x) for x in result]

    def events(self, job_id):
        return [dict(x) for x in self.conn.execute('SELECT * FROM print_events WHERE job_id=? ORDER BY id',
                                                   (job_id,)).fetchall()]
