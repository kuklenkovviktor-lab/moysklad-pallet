"""Use only the dedicated Compose database; never inherit a Render database URL."""
import os
import re
import sys
from urllib.parse import quote


def permitted_database(database):
    # This is the restored HOSTKEY production database. Do not accept arbitrary
    # names from untrusted environment configuration.
    return database in ("grit", "grit_migration_trial_20261111") or bool(
        re.fullmatch(r"restore_check_[a-z0-9_]{1,45}", database)
    )


def configure():
    password = os.environ.pop("POSTGRES_PASSWORD")
    database = os.getenv("GRIT_DATABASE_NAME", "grit")
    if not permitted_database(database):
        raise SystemExit("Invalid local database selection")
    os.environ["DATABASE_URL"] = (
        f"postgresql://grit:{quote(password, safe='')}@db:5432/{database}"
    )
    os.environ["WMS_REQUIRE_POSTGRES"] = "1"
    os.environ["WMS_MIGRATE_SQLITE_FROM_S3"] = "0"


if __name__ == "__main__":
    configure()
    os.execvp(sys.argv[1], sys.argv[1:])
