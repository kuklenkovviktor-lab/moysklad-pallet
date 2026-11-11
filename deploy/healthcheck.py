"""Readiness independent of external API credentials; no schema initialization."""
import json
import os
from urllib.request import urlopen
from urllib.parse import quote
import psycopg

from deploy.entrypoint import permitted_database


def check():
    with urlopen("http://127.0.0.1:8000/api/startup", timeout=3) as response:
        if not json.load(response).get("ready"):
            raise RuntimeError("Application is not ready")
    password = quote(os.environ["POSTGRES_PASSWORD"], safe="")
    database = os.getenv("GRIT_DATABASE_NAME", "grit")
    if not permitted_database(database):
        raise RuntimeError("Invalid local database selection")
    with psycopg.connect(
        f"postgresql://grit:{password}@db:5432/{database}", connect_timeout=3
    ) as db:
        if db.execute("SELECT 1").fetchone() != (1,):
            raise RuntimeError("Database probe failed")


if __name__ == "__main__":
    try:
        check()
    except Exception:
        raise SystemExit(1)
