import json
from urllib.request import urlopen
import psycopg
from trial_entrypoint import database_url

try:
    with urlopen('http://127.0.0.1:8000/api/startup',timeout=3) as response:
        assert json.load(response).get('ready')
    with psycopg.connect(database_url(),connect_timeout=3) as db:
        assert db.execute('SELECT 1').fetchone()==(1,)
except Exception:
    raise SystemExit(1)
