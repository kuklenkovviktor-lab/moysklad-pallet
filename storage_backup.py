import os
import sqlite3
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config


DB_PATH = os.getenv("WMS_DB_PATH", "/tmp/wms.db")

# Primary storage: Yandex Object Storage.
BUCKET = os.getenv("YC_S3_BUCKET", "").strip()
ACCESS_KEY_ID = os.getenv("YC_S3_ACCESS_KEY_ID", "").strip()
SECRET_ACCESS_KEY = os.getenv("YC_S3_SECRET_ACCESS_KEY", "").strip()
ENDPOINT = os.getenv("YC_S3_ENDPOINT", "https://storage.yandexcloud.net").strip()
REGION = os.getenv("YC_S3_REGION", "ru-central1").strip()
PREFIX = os.getenv("YC_S3_PREFIX", "mini-wms").strip().strip("/")

# Optional independent S3-compatible storage. It can be another provider entirely.
SECONDARY_BUCKET = os.getenv("WMS_SECONDARY_S3_BUCKET", "").strip()
SECONDARY_ACCESS_KEY_ID = os.getenv("WMS_SECONDARY_S3_ACCESS_KEY_ID", "").strip()
SECONDARY_SECRET_ACCESS_KEY = os.getenv("WMS_SECONDARY_S3_SECRET_ACCESS_KEY", "").strip()
SECONDARY_ENDPOINT = os.getenv("WMS_SECONDARY_S3_ENDPOINT", "").strip()
SECONDARY_REGION = os.getenv("WMS_SECONDARY_S3_REGION", "auto").strip() or "auto"
SECONDARY_PREFIX = os.getenv("WMS_SECONDARY_S3_PREFIX", "onix-wms").strip().strip("/")

INTERVAL = max(60, int(os.getenv("WMS_BACKUP_INTERVAL_SECONDS", "300") or 300))
ARCHIVE_INTERVAL = max(3600, int(os.getenv("WMS_ARCHIVE_INTERVAL_SECONDS", "21600") or 21600))
RETENTION_DAYS = max(1, int(os.getenv("WMS_BACKUP_RETENTION_DAYS", "30") or 30))
SECONDARY_INTERVAL = max(3600, int(os.getenv("WMS_SECONDARY_BACKUP_INTERVAL_SECONDS", "86400") or 86400))
FAIL_CLOSED_RESTORE = os.getenv("WMS_FAIL_CLOSED_RESTORE", "1").strip().lower() in ("1", "true", "yes", "on")


class RestoreRequiredError(RuntimeError):
    """Raised when a local DB is missing and no verified backup can be restored."""


_STATUS_LOCK = threading.RLock()
_WORKER_STARTED = False
_LAST_SOURCE_STAMP = None
_LAST_ARCHIVE_AT = 0.0
_LAST_CLEANUP_AT = 0.0
_LAST_SECONDARY_AT = 0.0
_STATUS = {
    "configured": False,
    "secondary_configured": False,
    "running": False,
    "last_attempt_at": None,
    "last_backup_at": None,
    "last_archive_at": None,
    "last_restore_at": None,
    "last_restore_source": None,
    "last_error": "",
    "secondary_last_backup_at": None,
    "secondary_last_error": "",
    "phase": "idle",
}


def configured():
    return bool(
        BUCKET
        and ACCESS_KEY_ID
        and SECRET_ACCESS_KEY
        and not os.getenv("DATABASE_URL", "").strip()
    )


def secondary_configured():
    return bool(
        SECONDARY_BUCKET
        and SECONDARY_ACCESS_KEY_ID
        and SECONDARY_SECRET_ACCESS_KEY
        and SECONDARY_ENDPOINT
        and not os.getenv("DATABASE_URL", "").strip()
    )


def _client(endpoint, region, access_key_id, secret_access_key):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=Config(
            connect_timeout=5,
            read_timeout=20,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _primary_client():
    return _client(ENDPOINT, REGION, ACCESS_KEY_ID, SECRET_ACCESS_KEY)


def _secondary_client():
    return _client(
        SECONDARY_ENDPOINT,
        SECONDARY_REGION,
        SECONDARY_ACCESS_KEY_ID,
        SECONDARY_SECRET_ACCESS_KEY,
    )


def _latest_key(prefix):
    return f"{prefix}/latest/wms.db"


def _archive_key(prefix, now):
    return f"{prefix}/archive/{now:%Y-%m-%d}/{now:%H%M%S}-wms.db"


def _secondary_daily_key(prefix, now):
    return f"{prefix}/daily/{now:%Y-%m-%d}/wms.db"


def _source_stamp():
    values = []
    for path in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        try:
            stat = os.stat(path)
            values.append((path, stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            continue
    return tuple(values)


def _verify_sqlite(path):
    conn = sqlite3.connect(path, timeout=10)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if not row or str(row[0]).lower() != "ok":
            raise RuntimeError("SQLite integrity_check failed")
        # A restored ONIX WMS DB must contain the operational employee table.
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='employees'"
        ).fetchone()
        if not table:
            raise RuntimeError("Backup is not an initialized ONIX WMS database")
    finally:
        conn.close()


def _snapshot_file():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(DB_PATH)

    fd, snapshot_path = tempfile.mkstemp(prefix="onix-wms-", suffix=".db")
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
        _verify_sqlite(snapshot_path)
        return snapshot_path
    except Exception:
        try:
            os.remove(snapshot_path)
        except OSError:
            pass
        raise


def _cleanup_primary_archives(s3, now):
    global _LAST_CLEANUP_AT
    if now.timestamp() - _LAST_CLEANUP_AT < 6 * 3600:
        return

    cutoff = now - timedelta(days=RETENTION_DAYS)
    archive_prefix = f"{PREFIX}/archive/"
    token = None
    to_delete = []

    while True:
        kwargs = {"Bucket": BUCKET, "Prefix": archive_prefix, "MaxKeys": 1000}
        if token:
            kwargs["ContinuationToken"] = token
        response = s3.list_objects_v2(**kwargs)
        for obj in response.get("Contents", []) or []:
            modified = obj.get("LastModified")
            if modified and modified.astimezone(timezone.utc) < cutoff:
                to_delete.append({"Key": obj["Key"]})
                if len(to_delete) == 1000:
                    s3.delete_objects(Bucket=BUCKET, Delete={"Objects": to_delete, "Quiet": True})
                    to_delete = []
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not token:
            break

    if to_delete:
        s3.delete_objects(Bucket=BUCKET, Delete={"Objects": to_delete, "Quiet": True})
    _LAST_CLEANUP_AT = now.timestamp()


def backup_now(force=False):
    global _LAST_SOURCE_STAMP, _LAST_ARCHIVE_AT, _LAST_SECONDARY_AT

    now = datetime.now(timezone.utc)
    attempt = now.isoformat()
    with _STATUS_LOCK:
        _STATUS.update({
            "configured": configured(),
            "secondary_configured": secondary_configured(),
            "last_attempt_at": attempt,
            "phase": "preparing",
        })

    if not configured():
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": False,
                "last_error": "Primary Object Storage backup is not configured",
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

    archive_due = force or not _LAST_ARCHIVE_AT or now.timestamp() - _LAST_ARCHIVE_AT >= ARCHIVE_INTERVAL
    secondary_due = secondary_configured() and (
        force or not _LAST_SECONDARY_AT or now.timestamp() - _LAST_SECONDARY_AT >= SECONDARY_INTERVAL
    )
    changed = stamp != _LAST_SOURCE_STAMP

    # Healthy no-op. Do not hammer Object Storage every minute when the DB is unchanged.
    if not force and not changed and not archive_due and not secondary_due:
        with _STATUS_LOCK:
            _STATUS["phase"] = "idle"
        return True

    snapshot_path = _snapshot_file()
    primary_ok = False
    try:
        with _STATUS_LOCK:
            _STATUS["phase"] = "uploading-primary"

        s3 = _primary_client()
        s3.upload_file(snapshot_path, BUCKET, _latest_key(PREFIX))

        if archive_due:
            s3.upload_file(snapshot_path, BUCKET, _archive_key(PREFIX, now))
            _LAST_ARCHIVE_AT = now.timestamp()
            _cleanup_primary_archives(s3, now)
            with _STATUS_LOCK:
                _STATUS["last_archive_at"] = now.isoformat()

        _LAST_SOURCE_STAMP = stamp
        primary_ok = True
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_backup_at": now.isoformat(),
                "last_error": "",
                "phase": "primary-ok",
            })
    except Exception as exc:
        with _STATUS_LOCK:
            _STATUS.update({
                "configured": True,
                "last_error": str(exc),
                "phase": "idle",
            })
        return False

    if secondary_due:
        try:
            with _STATUS_LOCK:
                _STATUS["phase"] = "uploading-secondary"
            s3b = _secondary_client()
            s3b.upload_file(snapshot_path, SECONDARY_BUCKET, _latest_key(SECONDARY_PREFIX))
            s3b.upload_file(snapshot_path, SECONDARY_BUCKET, _secondary_daily_key(SECONDARY_PREFIX, now))
            _LAST_SECONDARY_AT = now.timestamp()
            with _STATUS_LOCK:
                _STATUS.update({
                    "secondary_last_backup_at": now.isoformat(),
                    "secondary_last_error": "",
                })
        except Exception as exc:
            # Primary backup remains valid. Surface the secondary failure separately.
            with _STATUS_LOCK:
                _STATUS["secondary_last_error"] = str(exc)
    finally:
        with _STATUS_LOCK:
            _STATUS["phase"] = "idle"
        try:
            os.remove(snapshot_path)
        except OSError:
            pass

    return primary_ok


def _restore_from(client, bucket, key, tmp, source_name):
    client.download_file(bucket, key, tmp)
    _verify_sqlite(tmp)
    os.replace(tmp, DB_PATH)
    now = datetime.now(timezone.utc).isoformat()
    with _STATUS_LOCK:
        _STATUS.update({
            "last_restore_at": now,
            "last_restore_source": source_name,
            "last_error": "",
            "phase": "idle",
        })
    return True


def restore_if_needed():
    if os.getenv("DATABASE_URL", "").strip() or os.path.exists(DB_PATH):
        return False

    if not configured():
        # Fresh/local setups may intentionally have no backup configured.
        # Production is protected once primary Object Storage is configured.
        return False

    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    errors = []

    primary_tmp = DB_PATH + ".restore-primary"
    try:
        with _STATUS_LOCK:
            _STATUS["phase"] = "restoring-primary"
        return _restore_from(
            _primary_client(),
            BUCKET,
            _latest_key(PREFIX),
            primary_tmp,
            "primary",
        )
    except Exception as exc:
        errors.append("primary: " + str(exc))
    finally:
        try:
            if os.path.exists(primary_tmp):
                os.remove(primary_tmp)
        except OSError:
            pass

    if secondary_configured():
        secondary_tmp = DB_PATH + ".restore-secondary"
        try:
            with _STATUS_LOCK:
                _STATUS["phase"] = "restoring-secondary"
            return _restore_from(
                _secondary_client(),
                SECONDARY_BUCKET,
                _latest_key(SECONDARY_PREFIX),
                secondary_tmp,
                "secondary",
            )
        except Exception as exc:
            errors.append("secondary: " + str(exc))
        finally:
            try:
                if os.path.exists(secondary_tmp):
                    os.remove(secondary_tmp)
            except OSError:
                pass

    message = "Local DB is missing and backup restore failed: " + " | ".join(errors)
    with _STATUS_LOCK:
        _STATUS.update({"last_error": message, "phase": "restore-failed"})

    if FAIL_CLOSED_RESTORE:
        raise RestoreRequiredError(message)
    return False


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
        time.sleep(INTERVAL if ok else 60)


def start_worker():
    global _WORKER_STARTED
    if _WORKER_STARTED or not configured():
        with _STATUS_LOCK:
            _STATUS["configured"] = configured()
            _STATUS["secondary_configured"] = secondary_configured()
        return False

    _WORKER_STARTED = True
    with _STATUS_LOCK:
        _STATUS.update({
            "running": True,
            "configured": True,
            "secondary_configured": secondary_configured(),
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "phase": "queued",
        })
    thread = threading.Thread(target=_worker, name="onix-wms-backup", daemon=True)
    thread.start()
    return True


def status():
    with _STATUS_LOCK:
        data = dict(_STATUS)
    data.update({
        "configured": configured(),
        "secondary_configured": secondary_configured(),
        "interval_seconds": INTERVAL,
        "archive_interval_seconds": ARCHIVE_INTERVAL,
        "retention_days": RETENTION_DAYS,
        "secondary_interval_seconds": SECONDARY_INTERVAL,
        "fail_closed_restore": FAIL_CLOSED_RESTORE,
    })
    return data
