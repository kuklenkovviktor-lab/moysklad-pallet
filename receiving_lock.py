"""Serialize receipt read/modify/write and completion within this WMS deployment.

Does not coordinate a different database or manual edits in MoySklad itself.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import threading

_LOCKS={}
_GUARD=threading.Lock()


@contextmanager
def receipt_write_lock(receipt_id):
    import wms
    key='grit:enter:'+str(receipt_id)
    if wms.DATABASE_URL:
        with wms.db_connect() as conn:
            cur=conn.cursor()
            try:
                cur.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,))
                yield
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                cur.close()
    else:
        # Development SQLite: a per-document thread lock plus OS file lock on
        # Unix. Windows development Flask is supported in single-process mode.
        digest=hashlib.sha256(key.encode()).hexdigest()
        with _GUARD:
            lock=_LOCKS.setdefault((wms.DB_PATH,digest),threading.Lock())
        with lock:
            try:
                import fcntl
            except ImportError:
                yield
                return
            folder=Path(wms.DB_PATH+'.receipt-locks')
            folder.mkdir(mode=0o700,parents=True,exist_ok=True)
            with (folder/digest).open('a') as stream:
                fcntl.flock(stream,fcntl.LOCK_EX)
                try: yield
                finally: fcntl.flock(stream,fcntl.LOCK_UN)
