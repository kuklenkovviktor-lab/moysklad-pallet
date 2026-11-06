"""Local HOSTKEY operations only; construct the DSN without URI interpolation."""
import os
import sys
from psycopg.conninfo import make_conninfo


def local_dsn(environment):
    expected={'PGHOST':'db','PGPORT':'5432','PGUSER':'grit','PGDATABASE':'grit'}
    if any(environment.get(key)!=value for key,value in expected.items()):
        raise ValueError('Operations helper permits only the local db:5432/grit database')
    password=environment.get('PGPASSWORD')
    if not password:
        raise ValueError('Local database credential is missing')
    return make_conninfo(host='db',port=5432,user='grit',dbname='grit',password=password)


if __name__=='__main__':
    try:
        os.environ['GRIT_MIGRATION_DSN']=local_dsn(os.environ)
    except ValueError:
        raise SystemExit('Local operations configuration invalid; no connection attempted') from None
    os.execv(sys.executable,[sys.executable,'/tools/migration_tool.py',*sys.argv[1:]])
