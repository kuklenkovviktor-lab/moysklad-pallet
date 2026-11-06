"""Receive an external URL over stdin, never Docker configuration or argv."""
import json
import os
import sys
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from migration_tool import Refused, export


def external_dsn(value):
    if not isinstance(value,str) or any(c.isspace() for c in value) or len(value)>16000:
        raise Refused('Paste ONE PostgreSQL URL, without quotes, spaces or shell commands')
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in ('postgres','postgresql') or not parsed.hostname:
            raise ValueError()
        fields=conninfo_to_dict(value)
        if not fields.get('user') or not fields.get('dbname') or not fields.get('password'):
            raise ValueError()
        if any(k not in {'host','port','user','dbname','password','sslmode','sslrootcert'} for k in fields):
            raise ValueError()
        if fields.get('sslmode','verify-full')!='verify-full':
            raise ValueError()
        fields.update(sslmode='verify-full',sslrootcert='/etc/ssl/certs/ca-certificates.crt',connect_timeout='10',options='-c default_transaction_read_only=on')
        return make_conninfo(**fields)
    except Exception:
        # Never echo parsing errors: libpq can include the original password.
        raise Refused('Invalid external URL; use a percent-encoded PostgreSQL URL with verified TLS') from None


def main():
    os.umask(0o077)
    try:
        payload=json.loads(sys.stdin.buffer.read(65537))
        dsn=external_dsn(payload['url'])
        destination=payload['destination']
        if not destination.startswith('/bundles/') or '/' in destination[len('/bundles/'):]:
            raise Refused('Invalid destination')
        export(dsn,destination)
        print('Export verified: custom archive, consistent row fingerprints, SHA-256. Source was read-only.')
    except Exception as error:
        detail=str(error).lower()  # Classify locally; NEVER print the raw error.
        category='SOURCE_OR_ARCHIVE'
        if isinstance(error,FileExistsError):category='DESTINATION_EXISTS'
        elif isinstance(error,Refused):category='URL_OR_SCHEMA_OR_ARCHIVE'
        elif 'could not translate host' in detail or 'name or service not known' in detail:category='DNS'
        elif 'certificate' in detail or 'ssl' in detail:category='TLS'
        elif 'authentication' in detail or 'password' in detail:category='AUTHENTICATION'
        elif 'timeout' in detail or 'connection refused' in detail or 'network is unreachable' in detail:category='TCP_OR_FIREWALL'
        raise SystemExit('GRIT_EXPORT_FAILED:'+category) from None


if __name__=='__main__': main()
