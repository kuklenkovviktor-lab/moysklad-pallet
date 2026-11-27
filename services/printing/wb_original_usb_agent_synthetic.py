"""PR211 original-PDF synthetic USB client, using unchanged PR210 RAW algorithm.

No auto-start, no production WB, no arbitrary server URLs. Factory-injected
API is tested with Flask test client and FAKE Windows spooler. Physical USB
printing of an actual WB sticker is intentionally NOT enabled here.
"""
import base64
import binascii
import hashlib
import json
from pathlib import Path
import re

from services.printing.grit_usb_print_pilot import (
    TEST_PRINTER, check_printer, _send_exactly_one, _write_once, _update_journal,
    PilotError, _utc_now,
)

HEADER=(b'SIZE 58 mm,40 mm\r\nGAP 2 mm,0 mm\r\n'
        b'DIRECTION 1\r\nCLS\r\nBITMAP 0,0,58,320,0,')
TAIL=b'\r\nPRINT 1,1\r\n'
RAW_LEN=len(HEADER)+18560+len(TAIL)


def validate_original_bitmap(payload):
    """Never execute arbitrary TSPL delivered by a server."""
    if (not isinstance(payload,bytes) or len(payload)!=RAW_LEN
            or not payload.startswith(HEADER) or not payload.endswith(TAIL)):
        raise PilotError('UNEXPECTED_ORIGINAL_BITMAP_COMMAND')
    return payload


def decode_claim(data):
    if not isinstance(data,dict) or data.get('synthetic') is not True:
        raise PilotError('ONLY_SYNTHETIC_RESPONSE_ALLOWED')
    if data.get('physical_printing') is not False:
        raise PilotError('PHYSICAL_FLAG_FORBIDDEN')
    if data.get('state') == 'EMPTY':
        return None
    if (data.get('state')!='CLAIMED'
            or data.get('kind')!='ORIGINAL_PDF_BITMAP_SYNTHETIC_ONLY'):
        raise PilotError('UNEXPECTED_CLAIM_KIND')
    ticket=data.get('ticket_id')
    digest=data.get('payload_sha256')
    encoded=data.get('payload_b64')
    if (not isinstance(ticket,str) or not re.fullmatch(r'[0-9a-f]{32}',ticket)
            or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest)
            or not isinstance(encoded,str) or len(encoded)>30000):
        raise PilotError('UNTRUSTED_CLAIM_ENVELOPE')
    try:
        payload=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error) as exc:
        raise PilotError('MALFORMED_BITMAP_DATA') from exc
    validate_original_bitmap(payload)
    if hashlib.sha256(payload).hexdigest()!=digest:
        raise PilotError('ORIGINAL_BITMAP_DIGEST_MISMATCH')
    return ticket,payload,digest


def submit_one_synthetic_claim(api, spooler, *, journal_dir):
    """One claim, one possible RAW write, permanent journal, no recovery retry.

    api(path, payload) is a trusted authenticated loopback test-client callable.
    Only sends recognized synthetic original PDF pages and *never* handles
    production credentials. The caller may not choose a printer dynamically.
    """
    check_printer(spooler,TEST_PRINTER)
    info=api('/synthetic/pr211/agent/claim',{})
    claimed=decode_claim(info)
    if claimed is None:
        return 'EMPTY'
    ticket,payload,digest=claimed
    path=Path(journal_dir)/(ticket+'.json')
    record=dict(ticket_id=ticket,printer=TEST_PRINTER,
                payload_sha256=digest,kind='ORIGINAL_PDF_SYNTHETIC_ONLY',
                physical_print_confirmed=False,state='CLAIMED',
                created_utc=_utc_now())
    _write_once(path,record)  # before RAW; persistent no-retry marker
    try:
        check_printer(spooler,TEST_PRINTER)
        job_id=_send_exactly_one(spooler,TEST_PRINTER,payload)
        _update_journal(path,record,'SUBMITTED_TO_WINDOWS',spool_job_id=job_id)
    except Exception as exc:
        try:
            _update_journal(path,record,'UNKNOWN')
        except Exception:
            pass
        try:
            api('/synthetic/pr211/agent/report',dict(ticket_id=ticket,result='UNKNOWN'))
        except Exception:
            pass
        raise PilotError('RAW_OUTCOME_UNKNOWN_NO_AUTOMATIC_RETRY') from exc
    try:
        acknowledgment=api('/synthetic/pr211/agent/report',
            dict(ticket_id=ticket,result='SUBMITTED'))
        if acknowledgment.get('state')!='SUBMITTED':
            raise PilotError('NO_SERVER_ACK')
    except Exception as exc:
        # Journal already proves this ticket was submitted to Windows.
        # Even if report is lost, NEVER print it a second time.
        raise PilotError('SUBMITTED_TO_WINDOWS_SERVER_ACK_UNKNOWN_NO_RETRY') from exc
    return 'SUBMITTED_TO_WINDOWS'
