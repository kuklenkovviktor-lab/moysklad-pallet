import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


DB_PATH = os.getenv("WMS_DB_PATH", "/tmp/wms.db")
BUCKET = os.getenv("YC_S3_BUCKET", "").strip()
ACCESS_KEY_ID = os.getenv("YC_S3_ACCESS_KEY_ID", "").strip()
SECRET_ACCESS_KEY = os.getenv("YC_S3_SECRET_ACCESS_KEY", "").strip()
ENDPOINT = os.getenv("YC_S3_ENDPOINT", "https://storage.yandexcloud.net").strip()
REGION = os.getenv("YC_S3_REGION", "ru-central1").strip()
PREFIX = os.getenv("YC_S3_PREFIX", "mini-wms").strip().strip("/")
INTERVAL = max(60, int(os.getenv("WMS_BACKUP_INTERVAL_SECONDS", "300") or 300))

_STATUS_LOCK = threading.RLock()
_WORKER_STARTED = False
_LAST_SOURCE_STAMP = None
_LAST_DAILY = None
_STATUS = {
    "configured": False,
    "running": False,
    "last_attempt_at": None,
    "last_backup_at": None,
    "last_restore_at": None,
    "last_error": "",
    "latest_key": None,
    "phase": "idle",
}


class RestoreRequiredError(RuntimeError):
    """Local SQLite is missing and the configured backup could not be restored."""


def configured():
    return bool(BUCKET and ACCESS_KEY_ID and SECRET_ACCESS_KEY and not os.getenv("DATABASE_URL", "").strip())


def _client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_ACCESS_KEY,
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 1, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _latest_key():
    return f"{PREFIX}/latest/wms.db"


def _daily_key(day):
    return f"{PREFIX}/daily/{day}/wms.db"


def _source_stamp():
    values = []
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        try:
            stat = os.stat(path)
            values.append((path, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            continue
    return tuple(values)


def _snapshot_file():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(DB_PATH)

    fd, snapshot_path = tempfile.mkstemp(prefix="mini-wms-", suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH, timeout=3)
        dst = sqlite3.connect(snapshot_path, timeout=3)
        try:
            src.execute("PRAGMA busy_timeout=2000")
            deadline = time.monotonic() + 10.0

            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise TimeoutError("SQLite backup snapshot exceeded 10 seconds")

            # Small chunks release the source database between steps. If the DB
            # is busy, fail this backup attempt instead of stalling live WMS.
            src.backup(dst, pages=256, progress=progress, sleep=0.05)
            dst.commit()
        finally:
            dst.close()
            src.close()
        return snapshot_path
    except Exception:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        raise


def backup_now(force=False):
    global _LAST_SOURCE_STAMP, _LAST_DAILY

    attempt = datetime.now(timezone.utc).isoformat()
    with _STATUS_LOCK:
        _STATUS.update({
            "configured": configured(),
            "last_attempt_at": attempt,
            "phase": "preparing",
        })

    if not configured():
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": False,
                "last_error": "Object Storage backup is not configured",
                "phase": "idle",
            })
        return False

    stamp = _source_stamp()
    if not stamp:
        with _STATUS_LOCK:
            _STATUS.update({
                "last_error": f"SQLite file not found at {DB_PATH}",
                "phase": "idle",
            })
        return False
    if not force and stamp == _LAST_SOURCE_STAMP:
        with _STATUS_LOCK:
            _STATUS["phase"] = "idle"
        return False

    snapshot_path = _snapshot_file()
    try:
        with _STATUS_LOCK:
            _STATUS["phase"] = "uploading"
        s3 = _client()
        latest = _latest_key()
        s3.upload_file(snapshot_path, BUCKET, latest)

        today = datetime.now(timezone.utc).date().isoformat()
        if _LAST_DAILY != today:
            s3.upload_file(snapshot_path, BUCKET, _daily_key(today))
            _LAST_DAILY = today

        _LAST_SOURCE_STAMP = stamp
        now = datetime.now(timezone.utc).isoformat()
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_backup_at": now,
                "last_error": "",
                "latest_key": latest,
                "phase": "idle",
            })
        return True
    except Exception as exc:
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_error": str(exc),
                "phase": "idle",
            })
        return False
    finally:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass


def restore_if_needed():
    # Existing local DB or PostgreSQL: nothing to restore.
    if os.getenv("DATABASE_URL", "").strip() or os.path.exists(DB_PATH):
        return False

    # If backup is not configured, preserve current behavior for local/dev setups.
    if not configured():
        return False

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    tmp = DB_PATH + ".restore"
    try:
        with _STATUS_LOCK:
            _STATUS["phase"] = "restoring"

        _client().download_file(BUCKET, _latest_key(), tmp)

        # Refuse to replace the working DB path with an unreadable/corrupt SQLite file.
        conn = sqlite3.connect(tmp, timeout=10)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError("Backup SQLite integrity_check failed")
        finally:
            conn.close()

        os.replace(tmp, DB_PATH)
        now = datetime.now(timezone.utc).isoformat()
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_restore_at": now,
                "last_error": "",
                "latest_key": _latest_key(),
                "phase": "idle",
            })
        return True
    except Exception as exc:
        message = "Local DB is missing and backup restore failed: " + str(exc)
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_error": message,
                "phase": "restore-failed",
            })
        # Critical protection: do NOT continue to init_wms_db(), otherwise a new
        # empty DB could be created and later overwrite the valid remote backup.
        raise RestoreRequiredError(message) from exc
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _worker():
    first = True
    while True:
        ok = False
        try:
            ok = backup_now(force=first)
        except Exception as exc:
            with _STATUS_LOCK:
                _STATUS.update({"last_error": str(exc), "phase": "idle"})
        first = False
        # При ошибке пробуем снова через минуту, после успешной копии — по расписанию.
        time.sleep(INTERVAL if ok else 60)


def start_worker():
    global _WORKER_STARTED
    if _WORKER_STARTED or not configured():
        with _STATUS_LOCK:
            _STATUS["configured"] = configured()
        return False

    _WORKER_STARTED = True
    # Ставим отметку ДО запуска потока. Так health сразу показывает,
    # что первый backup действительно поставлен в очередь.
    with _STATUS_LOCK:
        _STATUS.update({
            "running": True,
            "configured": True,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "phase": "queued",
        })
    thread = threading.Thread(target=_worker, name="wms-object-storage-backup", daemon=True)
    thread.start()
    return True


def status():
    with _STATUS_LOCK:
        data = dict(_STATUS)
    data["configured"] = configured()
    data["interval_seconds"] = INTERVAL
    data["bucket"] = BUCKET or None
    return data
