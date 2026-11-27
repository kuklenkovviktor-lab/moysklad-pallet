"""PostgreSQL 17 isolated original-PDF outbox with PR210 RAW-spooler seam.

ONLY disposable PG17 named grit_pr211_synthetic_*. No Flask routes, no
production wiring, no physical USB. Complete transaction + one-way CLAIMED.
"""
import hashlib
import os
import re
import uuid
from datetime import datetime, timezone

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from services.printing.wb_pdf_original_label import (
    match_source_pdf, source_page_to_tspl, LabelSourceError,
)

DDL = (
    """CREATE TABLE IF NOT EXISTS synthetic_pr211_original_jobs (
         job_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, order_id TEXT NOT NULL,
         printer_id TEXT NOT NULL, line TEXT NOT NULL, sticker_hash TEXT NOT NULL,
         source_hash TEXT NOT NULL, payload_hash TEXT NOT NULL,
         cipher BYTEA, state TEXT NOT NULL
             CHECK(state IN ('STAGED','QUEUED','CLAIMED','SUBMITTED','UNKNOWN','BLOCKED')),
         ticket_id TEXT UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
         UNIQUE(account_id,order_id))""",
    """CREATE INDEX IF NOT EXISTS synthetic_pr211_jobs_next
         ON synthetic_pr211_original_jobs(printer_id,state,created_at)""",
    """CREATE TABLE IF NOT EXISTS synthetic_pr211_original_events (
         event_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES
         synthetic_pr211_original_jobs(job_id), event TEXT NOT NULL,
         created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
)


def _check(conn):
    # Without autocommit, an implicit SELECT transaction could make
    # a later conn.transaction() only a savepoint rather than a durable COMMIT.
    if not isinstance(conn,psycopg.Connection) or conn.autocommit is not True:
        raise RuntimeError('AUTOCOMMIT_CONNECTION_REQUIRED')
    if os.getenv('GRIT_SYNTHETIC_PG_TEST') != 'YES_ISOLATED':
        raise RuntimeError('ISOLATED_PG_FLAG_REQUIRED')
    target=os.getenv('PR211_SYNTHETIC_DATABASE_URL','')
    if not re.fullmatch(
        r'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_[a-z0-9_]+',
        target):
        raise RuntimeError('DISPOSABLE_PR211_DSN_REQUIRED')
    actual=conn.execute('SELECT current_database(),current_user').fetchone()
    if actual != (target.rsplit('/',1)[-1],'postgres'):
        raise RuntimeError('WRONG_DATABASE_OR_USER')
    if int(conn.execute('SHOW server_version_num').fetchone()[0]) // 10000 != 17:
        raise RuntimeError('POSTGRESQL_17_REQUIRED')
    return target


def _key(k):
    if type(k) is not bytes or len(k)!=32:
        raise ValueError('AES256_KEY_REQUIRED')


def _printer(pid):
    if not isinstance(pid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',pid):
        raise ValueError('INVALID_PRINTER_ID')


def _audit(conn,job_id,event):
    conn.execute("""INSERT INTO synthetic_pr211_original_events
        (event_id,job_id,event) VALUES (%s,%s,%s)""",
        (uuid.uuid4().hex,job_id,event))


def initialize(conn):
    _check(conn)
    with conn.transaction():
        for stmt in DDL:
            conn.execute(stmt)


def stage(conn,pdf,*,account_id,printer_id,verified_lines,key,clock=None,require_qr=False):
    _check(conn)
    _printer(printer_id)
    _key(key)
    # Match and rasterize only original 58x40 source pages, while DB is
    # read-only. No device access and no assumed/generated WB barcode.
    jobs=match_source_pdf(pdf,conn,account_id=account_id,
        verified_lines=verified_lines,clock=clock,adapt=lambda q:q.replace('?','%s'))
    if not 1<=len(jobs)<=100:
        raise LabelSourceError('INVALID_BATCH_SIZE')
    prepared=[]
    source_hash=hashlib.sha256(pdf).hexdigest()
    for job in jobs:
        payload=source_page_to_tspl(pdf,job.page_index)
        digest=hashlib.sha256(payload).hexdigest()
        aad=f'{job.account_id}\x1f{job.order_id}\x1f{printer_id}\x1f{job.line}\x1f{digest}'.encode()
        nonce=os.urandom(12)
        ciphertext=nonce+AESGCM(key).encrypt(nonce,payload,aad)
        prepared.append((uuid.uuid4().hex,job,digest,ciphertext))
    # Serializes competing stage requests across processes. Unique order key
    # protects against replay even after acknowledged SUBMITTED status.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('PR211-STAGE-'||%s,0))",
                     (printer_id,))
        count=conn.execute("""SELECT COUNT(*) FROM synthetic_pr211_original_jobs
            WHERE printer_id=%s AND state IN ('STAGED','QUEUED','CLAIMED','UNKNOWN')""",
            (printer_id,)).fetchone()[0]
        if count:
            raise LabelSourceError('PRINTER_HAS_UNRESOLVED_WORK')
        for job_id,job,digest,ciphertext in prepared:
            conn.execute("""INSERT INTO synthetic_pr211_original_jobs
                (job_id,account_id,order_id,printer_id,line,sticker_hash,
                 source_hash,payload_hash,cipher,state)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (job_id,job.account_id,job.order_id,printer_id,job.line,
                 hashlib.sha256(job.sticker_code.encode()).hexdigest(),
                 source_hash,digest,ciphertext,'STAGED' if require_qr else 'QUEUED'))
            _audit(conn,job_id,'ORIGINAL_STAGED')
    return len(prepared)


def claim(conn,*,printer_id,key,device_authenticated):
    _check(conn)
    _printer(printer_id)
    _key(key)
    if device_authenticated is not True:
        raise PermissionError('AUTHORIZED_DEVICE_REQUIRED')
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('PR211-CLAIM-'||%s,0))",
                     (printer_id,))
        blocked=conn.execute("""SELECT COUNT(*) FROM synthetic_pr211_original_jobs
             WHERE printer_id=%s AND state IN ('CLAIMED','UNKNOWN')""",
             (printer_id,)).fetchone()[0]
        if blocked:
            raise LabelSourceError('UNRESOLVED_PRINT_NO_RETRY')
        row=conn.execute("""SELECT job_id,account_id,order_id,line,payload_hash,
                   cipher,sticker_hash FROM synthetic_pr211_original_jobs
            WHERE printer_id=%s AND state='QUEUED'
            ORDER BY created_at,job_id FOR UPDATE SKIP LOCKED LIMIT 1""",
            (printer_id,)).fetchone()
        if not row:
            return None
        job_id,account,order,line,digest,cipher,sticker_hash=row
        # Production-schema source can change after PDF staging; reject
        # cancellation, cache changes or QR/sticker revocation before claim.
        # This check is inside the same transaction as the durable claim.
        source=conn.execute("""SELECT o.wb_status,o.local_status,
                   s.barcode,o.synced_at
            FROM wb_fbs_orders o JOIN wb_fbs_picking_queue q
              ON q.account_id=o.account_id AND q.order_id=o.order_id
            JOIN wb_fbs_stickers s
              ON s.account_id=o.account_id AND s.order_id=o.order_id
            WHERE o.account_id=%s AND o.order_id=%s FOR UPDATE OF o""",
            (account,order)).fetchall()
        valid=(len(source)==1 and source[0][0]=='confirm'
               and source[0][1]=='pending'
               and hashlib.sha256(str(source[0][2]).encode()).hexdigest()==sticker_hash)
        if valid:
            try:
                updated=source[0][3]
                if not isinstance(updated,datetime):
                    updated=datetime.fromisoformat(str(updated).replace('Z','+00:00'))
                age=(datetime.now(timezone.utc)-updated.astimezone(timezone.utc)).total_seconds()
                valid=(updated.tzinfo is not None and 0<=age<=180)
            except (ValueError,TypeError,OverflowError,AttributeError):
                valid=False
        if valid:
            # Ensure the ORIGINAL SKU still maps to one active cell on the
            # preapproved line, not a newly moved or ambiguous location.
            mapped=conn.execute("""SELECT l.label,l.active FROM
               wb_fbs_order_skus k
               JOIN fbs_catalog_barcodes c ON c.barcode=k.sku
               JOIN cell_sku_assignments a ON a.product_id=c.product_id
               JOIN replenishment_locations l ON l.id=a.location_id
               WHERE k.account_id=%s AND k.order_id=%s
                 AND c.is_pack=0 AND c.quantity=1""",
               (account,order)).fetchall()
            valid=(len(mapped)==1 and mapped[0][1]==1
                   and bool(re.match(r'^'+re.escape(line)+r'-?\d',
                                     str(mapped[0][0]).upper())))
        if not valid:
            conn.execute("""UPDATE synthetic_pr211_original_jobs
                SET state='BLOCKED' WHERE job_id=%s AND state='QUEUED'""",
                (job_id,))
            _audit(conn,job_id,'SOURCE_CHANGED_NO_PRINT')
            return None
        ticket=uuid.uuid4().hex
        conn.execute("""UPDATE synthetic_pr211_original_jobs SET state='CLAIMED',
            ticket_id=%s WHERE job_id=%s AND state='QUEUED'""",(ticket,job_id))
        _audit(conn,job_id,'CLAIMED_BEFORE_RAW')
    # A cryptographic failure leaves durable CLAIMED, never re-queues.
    aad=f'{account}\x1f{order}\x1f{printer_id}\x1f{line}\x1f{digest}'.encode()
    try:
        ciphertext=bytes(cipher)
        payload=AESGCM(key).decrypt(ciphertext[:12],ciphertext[12:],aad)
        if hashlib.sha256(payload).hexdigest()!=digest:
            raise ValueError('HASH_MISMATCH')
    except Exception as exc:
        raise LabelSourceError('CLAIMED_PAYLOAD_INVALID_NO_RETRY') from exc
    return ticket,payload


def report(conn,*,printer_id,ticket,result,device_authenticated):
    _check(conn)
    _printer(printer_id)
    if device_authenticated is not True:
        raise PermissionError('AUTHORIZED_DEVICE_REQUIRED')
    if (not isinstance(ticket,str) or not re.fullmatch('[0-9a-f]{32}',ticket)
            or result not in ('SUBMITTED','UNKNOWN')):
        raise ValueError('INVALID_ACK')
    with conn.transaction():
        row=conn.execute("""SELECT job_id FROM synthetic_pr211_original_jobs
            WHERE printer_id=%s AND ticket_id=%s AND state='CLAIMED'
            FOR UPDATE""",(printer_id,ticket)).fetchone()
        if not row:
            raise LabelSourceError('TICKET_NOT_CLAIMED_NO_RETRY')
        conn.execute("""UPDATE synthetic_pr211_original_jobs SET state=%s
            WHERE job_id=%s AND state='CLAIMED'""",(result,row[0]))
        _audit(conn,row[0],result)
    return result


def dispatch_fake_windows(conn,*,printer_id,key,device_authenticated,
                          fake_spooler):
    """Real PR210 _send_exactly_one algorithm on a FAKE Windows spooler.

    No physical I/O. Prevent server-provided raw TSPL or arbitrary printer
    selections; only offline tests call this with trusted fake_spooler.
    """
    if fake_spooler is None:
        raise ValueError('FAKE_SPOOLER_REQUIRED')
    from services.printing.grit_usb_print_pilot import _send_exactly_one, TEST_PRINTER
    result=claim(conn,printer_id=printer_id,key=key,
                 device_authenticated=device_authenticated)
    if result is None:
        return 'EMPTY'
    ticket,payload=result
    try:
        _send_exactly_one(fake_spooler,TEST_PRINTER,payload)
    except Exception:
        report(conn,printer_id=printer_id,ticket=ticket,
               result='UNKNOWN',device_authenticated=device_authenticated)
        raise LabelSourceError('WINDOWS_SPOOL_UNKNOWN_NO_RETRY') from None
    report(conn,printer_id=printer_id,ticket=ticket,
           result='SUBMITTED',device_authenticated=device_authenticated)
    return 'SUBMITTED'

def approve_worker_qr(conn, *, printer_id, worker_id, shift_id, signed_qr_valid):
    """A verified /fbs worker QR releases only this printer's staged batch.

    Caller must validate current badge+active shift+picking activity and
    actual signed printer QR against the stored printer digest on server.
    """
    _check(conn)
    _printer(printer_id)
    if (signed_qr_valid is not True or not isinstance(worker_id,str)
            or not isinstance(shift_id,str) or not worker_id or not shift_id):
        raise PermissionError('ACTIVE_EMPLOYEE_SHIFT_AND_PRINTER_QR_REQUIRED')
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended('PR211-CLAIM-'||%s,0))",
                     (printer_id,))
        if conn.execute("""SELECT COUNT(*) FROM synthetic_pr211_original_jobs
            WHERE printer_id=%s AND state IN ('CLAIMED','UNKNOWN')""",
            (printer_id,)).fetchone()[0]:
            raise LabelSourceError('UNRESOLVED_PRINT_NO_QR_RELEASE')
        rows=conn.execute("""UPDATE synthetic_pr211_original_jobs
            SET state='QUEUED' WHERE printer_id=%s AND state='STAGED'
            RETURNING job_id""",(printer_id,)).fetchall()
        for row in rows:
            _audit(conn,row[0],'WORKER_QR_'+hashlib.sha256(
                 (worker_id+'|'+shift_id).encode()).hexdigest()[:12])
    return len(rows)


def prune_printed_payloads(conn, *, retention_hours=24, manager_authorized):
    """Explicit cleanup: scrub ciphertext only, keep unique order + audit.

    NEVER delete in-flight/unknown jobs or automatically requeue anything.
    """
    _check(conn)
    if manager_authorized is not True:
        raise PermissionError('MANAGER_AUTH_REQUIRED')
    if type(retention_hours) is not int or not 1 <= retention_hours <= 720:
        raise ValueError('INVALID_RETENTION_HOURS')
    with conn.transaction():
        rows=conn.execute("""UPDATE synthetic_pr211_original_jobs
            SET cipher=NULL WHERE state IN ('SUBMITTED','BLOCKED')
              AND cipher IS NOT NULL AND created_at <
                  NOW() - make_interval(hours => %s)
            RETURNING job_id""",(retention_hours,)).fetchall()
        for (job,) in rows:
            _audit(conn,job,'CIPHERTEXT_ERASED')
    return len(rows)


def queue_status(conn, *, printer_id):
    """Manager/worker presentation metadata, never return sticker or PDF."""
    _check(conn)
    _printer(printer_id)
    return {state:int(count) for state,count in conn.execute(
        """SELECT state,COUNT(*) FROM synthetic_pr211_original_jobs
           WHERE printer_id=%s GROUP BY state""",(printer_id,)).fetchall()}
