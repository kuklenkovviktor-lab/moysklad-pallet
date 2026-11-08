"""Receive an external URL over stdin, never Docker configuration or argv."""
import json
import os
import sys
from urllib.parse import urlsplit

from psycopg.conninfo import conninfo_to_dict, make_conninfo
from migration_tool import Refused, export, check_source, DiagnosticFailure, at_stage, failure_category


def external_dsn(value):
    if not isinstance(value,str) or any(c.isspace() for c in value) or len(value)>16000:
        raise Refused('Paste ONE PostgreSQL URL, without quotes, spaces or shell commands')
    try:
        parsed=urlsplit(value)
        if parsed.scheme not in ('postgres','postgresql') or not parsed.hostname:
            raise ValueError()
        fields=conninfo_to_dict(value)
        if not fields.get('user') or not fields.get('dbname') or not fields.get('password') or fields.get('host','').startswith('/'):
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
    phase='INPUT'
    try:
        with at_stage('INPUT'):
            payload=json.loads(sys.stdin.buffer.read(65537))
            dsn=external_dsn(payload['url'])
            action=payload.get('action','export')
            if action not in ('export','check-source'):raise Refused('Invalid action')
        if action=='check-source':
            phase='SOURCE_CHECK'
            result=check_source(dsn)
            print('Source check passed: PostgreSQL '+str(result['server_version'])+'; native clients major '+str(result['client_major'])+'; readable public tables '+str(result['tables_checked'])+'. No dump created; full export may still encounter other errors.')
        else:
            with at_stage('INPUT'):
                destination=payload['destination']
                if not destination.startswith('/bundles/') or '/' in destination[len('/bundles/'):]:
                    raise Refused('Invalid destination')
            phase='EXPORT'
            export(dsn,destination)
            print('Export verified: custom archive, consistent row fingerprints, SHA-256. Source was read-only.')
    except Exception as error:
        diagnostic=error if isinstance(error,DiagnosticFailure) else DiagnosticFailure(phase,failure_category(error))
        raise SystemExit(str(diagnostic)) from None


if __name__=='__main__': main()
