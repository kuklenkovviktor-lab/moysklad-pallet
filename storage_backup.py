import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timezone

import boto3
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
    "last_backup_at": None,
    "last_restore_at": None,
    "last_error": "",
    "latest_key": None,
}


def configured():
    return bool(BUCKET and ACCESS_KEY_ID and SECRET_ACCESS_KEY and not os.getenv("DATABASE_URL", "").strip())


def _client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        region_name=REGION,
        aws_access_key_id=ACCESS_KEY_ID,
        aws_secret_access_key=SECRET_ACCESS_KEY,
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
        src = sqlite3.connect(DB_PATH, timeout=20)
        dst = sqlite3.connect(snapshot_path, timeout=20)
        try:
            src.backup(dst)
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

    if not configured():
        with _STATUS_LOCK:
            _STATUS["configured"] = False
        return False

    stamp = _source_stamp()
    if not stamp:
        return False
    if not force and stamp == _LAST_SOURCE_STAMP:
        return False

    snapshot_path = _snapshot_file()
    try:
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
            })
        return True
    except Exception as exc:
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_error": str(exc),
            })
        return False
    finally:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass


def restore_if_needed():
    if os.getenv("DATABASE_URL", "").strip() or os.path.exists(DB_PATH) or not configured():
        return False

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    tmp = DB_PATH + ".restore"
    try:
        _client().download_file(BUCKET, _latest_key(), tmp)
        os.replace(tmp, DB_PATH)
        now = datetime.now(timezone.utc).isoformat()
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_restore_at": now,
                "last_error": "",
                "latest_key": _latest_key(),
            })
        return True
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NoSuchBucket"):
            with _STATUS_LOCK:
                _STATUS.update({
                    "configured": True,
                    "last_error": "" if code != "NoSuchBucket" else "Bucket not found",
                })
            return False
        with _STATUS_LOCK:
            _STATUS.update({"configured": True, "last_error": str(exc)})
        return False
    except Exception as exc:
        with _STATUS_LOCK:
            _STATUS.update({"configured": True, "last_error": str(exc)})
        return False
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def _worker():
    with _STATUS_LOCK:
        _STATUS["running"] = True
        _STATUS["configured"] = configured()

    # Первый снимок делаем вскоре после старта, а не ждём полный интервал.
    time.sleep(15)
    while True:
        try:
            backup_now(force=False)
        except Exception as exc:
            with _STATUS_LOCK:
                _STATUS["last_error"] = str(exc)
        time.sleep(INTERVAL)


def start_worker():
    global _WORKER_STARTED
    if _WORKER_STARTED or not configured():
        with _STATUS_LOCK:
            _STATUS["configured"] = configured()
        return False

    _WORKER_STARTED = True
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
