"""Encrypted, durable, fail-closed ORIGINAL PDF print outbox (offline prototype).

Caller MUST be authorized by the hosting WMS server before entering stage/claim.
No routes, print services, background tasks or production registration exist here.
Designed for disposable SQLite contract tests, not live WB deployment.
"""
from datetime import datetime, timezone
import hashlib
import os
import re
import sqlite3
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from services.printing.wb_pdf_original_label import (
    match_source_pdf, source_page_to_tspl, LabelSourceError,
)


def initialize(conn):
    """Only call against a disposable test DB; creates no production tables."""
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS synthetic_original_print_outbox (
        job_id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        order_id TEXT NOT NULL,
        printer_id TEXT NOT NULL,
        line TEXT NOT NULL,
        sticker_hash TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        payload_encrypted BLOB NOT NULL,
        state TEXT NOT NULL CHECK(state IN
          ('QUEUED','CLAIMED','SUBMITTED','UNKNOWN')),
        ticket_id TEXT UNIQUE,
        created_at TEXT NOT NULL,
        UNIQUE(account_id,order_id)
      );
      CREATE INDEX IF NOT EXISTS synthetic_original_print_next
        ON synthetic_original_print_outbox(printer_id,state,created_at);
      CREATE TABLE IF NOT EXISTS synthetic_original_print_audit (
        event_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL,
        event TEXT NOT NULL,
        recorded_at TEXT NOT NULL
      );
    """)


def _printer(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',value):
        raise ValueError("INVALID_TRUSTED_PRINTER_ID")
    return value


def _key(value):
    if not isinstance(value,bytes) or len(value)!=32:
        raise ValueError("ENCRYPTION_KEY_MUST_BE_32_BYTES")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _audit(conn,job,event):
    conn.execute("""INSERT INTO synthetic_original_print_audit VALUES(?,?,?,?)""",
                 (uuid.uuid4().hex,job,event,_now()))


def _no_active_work(conn, printer):
    row=conn.execute("""SELECT count(*) FROM synthetic_original_print_outbox
        WHERE printer_id=? AND state IN ('QUEUED','CLAIMED','UNKNOWN')""",
        (printer,)).fetchone()
    return row[0] == 0


def stage_verified_pdf(conn, pdf_bytes, *, account_id, printer_id,
                       verified_lines, key, clock=None):
    """Match complete PDF to real-schema fixture, stage encrypted ORIGINAL pages.

    Rejects whole batch when mapping, PDF, barcode, order or line is ambiguous.
    Protects duplicate order and refuses a second unresolved printer batch.
    All inserts are one SQL transaction; never sends data to a print device.
    """
    _printer(printer_id)
    _key(key)
    if not isinstance(conn,sqlite3.Connection):
        raise ValueError("ONLY_DISPOSABLE_SQLITE_PROTOTYPE")
    # Mapping and exact source are checked BEFORE starting a write transaction.
    jobs=match_source_pdf(pdf_bytes,conn,account_id=account_id,
                          verified_lines=verified_lines,clock=clock)
    if not jobs or len(jobs)>100:
        raise LabelSourceError('BATCH_SIZE_REFUSED')
    pdf_hash=hashlib.sha256(pdf_bytes).hexdigest()
    prepared=[]
    for job in jobs:
        payload=source_page_to_tspl(pdf_bytes,job.page_index)
        payload_hash=hashlib.sha256(payload).hexdigest()
        nonce=os.urandom(12)
        associated=f"{job.account_id}\x1f{job.order_id}\x1f{printer_id}\x1f{job.line}\x1f{payload_hash}".encode()
        encrypted=nonce+AESGCM(key).encrypt(nonce,payload,associated)
        prepared.append((uuid.uuid4().hex,job,payload_hash,encrypted))
    try:
        conn.execute("BEGIN IMMEDIATE")
        if not _no_active_work(conn,printer_id):
            raise LabelSourceError('PRINTER_ALREADY_HAS_UNRESOLVED_BATCH')
        for jobid,job,payload_hash,encrypted in prepared:
            conn.execute("""INSERT INTO synthetic_original_print_outbox
                (job_id,account_id,order_id,printer_id,line,sticker_hash,
                source_hash,payload_hash,payload_encrypted,state,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,'QUEUED',?)""",
                (jobid,job.account_id,job.order_id,printer_id,job.line,
                 hashlib.sha256(job.sticker_code.encode()).hexdigest(),
                 pdf_hash,payload_hash,encrypted,_now()))
            _audit(conn,jobid,'STAGED_ORIGINAL_PDF')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return len(prepared)


def claim_one(conn, *, printer_id, key, authorized_device):
    """One authorized agent ticket. Mark CLAIMED before exposing decrypted bytes.

    A process failure leaves CLAIMED; no lease expiry or automatic redelivery.
    Device authorization MUST be checked by the API/terminal host, not inferred.
    """
    _printer(printer_id)
    _key(key)
    if authorized_device is not True:
        raise PermissionError('DEVICE_AUTH_REQUIRED')
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing=conn.execute("""SELECT count(*) FROM synthetic_original_print_outbox
            WHERE printer_id=? AND state IN ('CLAIMED','UNKNOWN')""",
            (printer_id,)).fetchone()[0]
        if existing:
            raise LabelSourceError('UNRESOLVED_CLAIM_BLOCKS_FURTHER_PRINTING')
        row=conn.execute("""SELECT job_id,account_id,order_id,line,payload_hash,
                 payload_encrypted FROM synthetic_original_print_outbox
                 WHERE printer_id=? AND state='QUEUED'
                 ORDER BY created_at,job_id LIMIT 1""",(printer_id,)).fetchone()
        if not row:
            conn.commit()
            return None
        jobid,account,order,line,payload_hash,encrypted=row
        ticket=uuid.uuid4().hex
        conn.execute("""UPDATE synthetic_original_print_outbox SET state='CLAIMED',
           ticket_id=? WHERE job_id=? AND state='QUEUED'""",(ticket,jobid))
        _audit(conn,jobid,'CLAIMED_BEFORE_USB')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    associated=f"{account}\x1f{order}\x1f{printer_id}\x1f{line}\x1f{payload_hash}".encode()
    try:
        payload=AESGCM(key).decrypt(encrypted[:12],encrypted[12:],associated)
        if hashlib.sha256(payload).hexdigest()!=payload_hash:
            raise LabelSourceError('PAYLOAD_DIGEST_MISMATCH')
    except Exception as exc:
        # Claim remains durable. Never retry/roll back to QUEUED.
        raise LabelSourceError('CLAIMED_PAYLOAD_UNAVAILABLE_MANUAL_REVIEW') from exc
    return ticket,payload


def report(conn, *, printer_id, ticket_id, result, authorized_device):
    """Spooler ACK is SUBMITTED, not physical proof; no retries on UNKNOWN."""
    _printer(printer_id)
    if authorized_device is not True:
        raise PermissionError('DEVICE_AUTH_REQUIRED')
    if (not isinstance(ticket_id,str)
            or not re.fullmatch(r'[0-9a-f]{32}',ticket_id)
            or result not in ('SUBMITTED','UNKNOWN')):
        raise ValueError('INVALID_PRINT_REPORT')
    try:
        conn.execute("BEGIN IMMEDIATE")
        row=conn.execute("""SELECT job_id,state FROM synthetic_original_print_outbox
            WHERE ticket_id=? AND printer_id=?""",(ticket_id,printer_id)).fetchone()
        if not row or row[1]!='CLAIMED':
            raise LabelSourceError('CLAIM_NOT_ACTIVE_NO_REPRINT')
        conn.execute("""UPDATE synthetic_original_print_outbox
            SET state=? WHERE job_id=? AND state='CLAIMED'""",(result,row[0]))
        _audit(conn,row[0],result)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def deliver_one_with_bound_agent(conn, *, printer_id, key, authorized_device,
                                 submit_raw_once):
    """OFFLINE adapter seam for existing PR210 single-write spooler.

    submit_raw_once is a trusted, caller-provided TEST spool function, NOT a
    server-provided command or web-request body. This prototype is deliberately
    not registered as a WMS HTTP endpoint and never selects a real USB device.
    """
    if not callable(submit_raw_once):
        raise ValueError("TRUSTED_SPOOLER_CALLBACK_REQUIRED")
    claimed=claim_one(conn,printer_id=printer_id,key=key,
                      authorized_device=authorized_device)
    if claimed is None:
        return "EMPTY"
    ticket,payload=claimed
    try:
        # The original PDF bitmap is passed verbatim: never turn it back into
        # a text barcode or a hand-built imitation of a WB label.
        submitted=submit_raw_once(payload)
        if submitted is not True:
            raise RuntimeError("SPOOLER_DID_NOT_ACK")
    except Exception:
        report(conn,printer_id=printer_id,ticket_id=ticket,
               result="UNKNOWN",authorized_device=authorized_device)
        raise LabelSourceError("SPOOL_OUTCOME_UNKNOWN_NO_RETRY") from None
    report(conn,printer_id=printer_id,ticket_id=ticket,
           result="SUBMITTED",authorized_device=authorized_device)
    return "SUBMITTED"
