"""PR211 isolated PG17 printer-scoped device credentials.

No production registration, no real USB, no live WB data. Server owns every
device→printer binding. Tokens are shown exactly once upon manager enrollment.
"""
import hashlib
import hmac
import re
import secrets
import uuid
from datetime import datetime, timezone

from services.printing.wb_original_pg17_outbox import _check, _printer


class AuthenticationFailed(PermissionError):
    pass


def initialize(conn):
    _check(conn)
    with conn.transaction():
        conn.execute("""CREATE TABLE IF NOT EXISTS synthetic_pr211_print_devices(
          device_id TEXT PRIMARY KEY,
          printer_id TEXT NOT NULL,
          token_hash TEXT NOT NULL,
          active BOOLEAN NOT NULL DEFAULT TRUE,
          issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          revoked_at TIMESTAMPTZ
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS synthetic_pr211_devices_printer
          ON synthetic_pr211_print_devices(printer_id) WHERE active=TRUE""")


def enroll(conn, *, printer_id, manager_authorized):
    """Caller MUST authenticate a privileged manager before invoking."""
    _check(conn)
    _printer(printer_id)
    if manager_authorized is not True:
        raise AuthenticationFailed('MANAGER_AUTH_REQUIRED')
    token=secrets.token_urlsafe(48)
    device_id=uuid.uuid4().hex
    digest=hashlib.sha256(token.encode('ascii')).hexdigest()
    with conn.transaction():
        conn.execute("""INSERT INTO synthetic_pr211_print_devices
            (device_id,printer_id,token_hash) VALUES (%s,%s,%s)""",
            (device_id,printer_id,digest))
    return {'device_id':device_id,'token':token,'printer_id':printer_id}


def authenticate(conn, *, device_id, bearer_token):
    """Fail closed, constant-time token comparison, server-owned printer ID."""
    _check(conn)
    if not isinstance(device_id,str) or not re.fullmatch(r'[0-9a-f]{32}',device_id):
        raise AuthenticationFailed('DEVICE_AUTH_FAILED')
    if (not isinstance(bearer_token,str) or not re.fullmatch(
            r'[A-Za-z0-9_-]{32,128}',bearer_token)):
        raise AuthenticationFailed('DEVICE_AUTH_FAILED')
    row=conn.execute("""SELECT printer_id,token_hash,active
        FROM synthetic_pr211_print_devices WHERE device_id=%s""",
        (device_id,)).fetchone()
    digest=hashlib.sha256(bearer_token.encode('ascii')).hexdigest()
    if not row or not hmac.compare_digest(row[1],digest) or row[2] is not True:
        raise AuthenticationFailed('DEVICE_AUTH_FAILED')
    return row[0]


def revoke(conn, *, device_id, manager_authorized):
    _check(conn)
    if manager_authorized is not True:
        raise AuthenticationFailed('MANAGER_AUTH_REQUIRED')
    if not isinstance(device_id,str) or not re.fullmatch(r'[0-9a-f]{32}',device_id):
        raise ValueError('INVALID_DEVICE_ID')
    with conn.transaction():
        row=conn.execute("""UPDATE synthetic_pr211_print_devices
            SET active=FALSE,revoked_at=NOW()
            WHERE device_id=%s AND active=TRUE RETURNING device_id""",
            (device_id,)).fetchone()
    return bool(row)


def status(conn):
    """Metadata only. Never expose tokens or password hashes."""
    _check(conn)
    rows=conn.execute("""SELECT device_id,printer_id,active,issued_at,
                                  revoked_at
        FROM synthetic_pr211_print_devices ORDER BY issued_at DESC""").fetchall()
    return [
        dict(device_id=r[0],printer_id=r[1],active=bool(r[2]),
             issued_at=r[3].isoformat(),revoked_at=r[4].isoformat() if r[4] else None)
        for r in rows
    ]
