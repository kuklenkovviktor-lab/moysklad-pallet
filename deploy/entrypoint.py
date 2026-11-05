"""Use only the dedicated Compose database; never inherit a Render database URL."""
import os
import sys
from urllib.parse import quote


def configure():
    password = os.environ.pop('POSTGRES_PASSWORD')
    os.environ['DATABASE_URL'] = f'postgresql://grit:{quote(password, safe="")}@db:5432/grit'
    os.environ['WMS_REQUIRE_POSTGRES'] = '1'
    os.environ['WMS_MIGRATE_SQLITE_FROM_S3'] = '0'


if __name__ == '__main__':
    configure()
    os.execvp(sys.argv[1], sys.argv[1:])
