"""Windows ONE-TIME synthetic original-like PDF physical acceptance.

An isolated PostgreSQL17 on HOSTKEY validates a real generated PDF through
manager/worker/device HTTP routes, then streams ONE original-page TSPL bitmap.
Windows prints only to dedicated non-default USB ONIX-TEST-XP460B via the same
PR210 one-write RAW function; journal is durable BEFORE WritePrinter.
Never touches real WB orders, live WMS, APIs, or production printers.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

# Keep imports sourced from current PR211 checkout, not other installations.
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from services.printing.grit_usb_print_pilot import (
    TEST_PRINTER, _spooler, check_printer, _send_exactly_one, _write_once,
    _update_journal, _utc_now, PilotError,
)
from services.printing.wb_original_usb_agent_synthetic import decode_claim

BRANCH='feature/pr211-wb-real-source-preflight'
REPO='https://github.com/kuklenkovviktor-lab/moysklad-pallet.git'
LABEL_MARKER='PR211_SYNTHETIC_PHYSICAL_PDF'
ATTEMPT_FILE='pr211_original_like_pdf_physical_once.json'


def marker():
    basedir=os.environ.get('LOCALAPPDATA','')
    if not basedir:
        raise PilotError('LOCALAPPDATA required for permanent no-retry journal')
    return Path(basedir)/'GRIT-WMS'/'PR211-Original-PDF'/ATTEMPT_FILE


def remote_command():
    # Repository branch/commands are constants. No shell input from user.
    script=(
        'set -Eeuo pipefail; '
        'DIR=$(mktemp -d /tmp/grit-pr211-physical-XXXXXX); '
        "trap 'rm -rf \"$DIR\"' EXIT; "
        f'git clone -q --depth 1 --branch {BRANCH} {REPO} "$DIR"; '
        'bash "$DIR/deploy/run_pr211_physical_pdf_server.sh" "$DIR"'
    )
    return 'bash -c '+shlex.quote(script)


def execute(spooler=None,popen=subprocess.Popen,state_path=None):
    path=Path(state_path) if state_path is not None else marker()
    if path.exists():
        raise PilotError(
            'PR211 physical test attempt already registered. '
            'NO REPRINT. Inspect the existing paper and Windows spool queue.')
    win=spooler or _spooler()
    usb=check_printer(win,TEST_PRINTER)  # BEFORE network or claim.
    print('GRIT PR211: TEST PRINTER',TEST_PRINTER,'USB',usb,flush=True)
    print('Synthetic WB-like PDF via REAL strict parser, PG17 and QR; ONE label',flush=True)
    cmd=['ssh','-T','grit',remote_command()]
    try:
        process=popen(cmd,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                      stderr=None,text=True,bufsize=1)
    except (OSError,ValueError) as exc:
        raise PilotError('SSH grit unavailable; NO USB job was sent') from exc

    sent_ack=False
    try:
        line=process.stdout.readline()
        if not line:
            raise PilotError('HOSTKEY did not provide a validated synthetic PDF claim')
        try:
            packet=json.loads(line)
        except (ValueError,TypeError) as exc:
            raise PilotError('Invalid server payload; no USB job') from exc
        if (packet.get('kind')!=LABEL_MARKER or
                packet.get('order')!='SYNTH-PR211-PDF-PHYSICAL-1' or
                packet.get('sticker')!='59298125228' or
                packet.get('line')!='А'):
            raise PilotError('Synthetic origin/line not verified; refuse print')
        claim=packet.get('claim')
        validated=decode_claim(claim)
        if validated is None:
            raise PilotError('No approved printer QR claim')
        ticket,payload,digest=validated
        record={
            'kind':LABEL_MARKER,'ticket':ticket,
            'printer':TEST_PRINTER,'usb_port':usb,
            'source':'ORIGINAL-LIKE-INVENTED-PDF-ONLY',
            'wb_account':None,'sha256':digest,
            'physical_print_confirmed':False,
            'state':'CLAIMED','created_utc':_utc_now(),
        }
        # CRITICAL: durable PERMANENT one-shot marker BEFORE Windows RAW write.
        _write_once(path,record)
        print('PR211: validated PDF bitmap; submitting exactly ONE RAW label',flush=True)
        try:
            # Re-check after claim before touching Windows print queue.
            check_printer(win,TEST_PRINTER)
            spool_id=_send_exactly_one(win,TEST_PRINTER,payload)
            _update_journal(path,record,'SUBMITTED_TO_WINDOWS',
                            spool_job_id=spool_id)
        except Exception as exc:
            try:
                _update_journal(path,record,'UNKNOWN')
            except Exception:
                pass
            raise PilotError('USB result UNKNOWN. DO NOT RETRY') from exc
        # Report only after spooler returns complete byte count and document close.
        # Physical paper output still needs operator's visual confirmation.
        process.stdin.write('SUBMITTED\n')
        process.stdin.flush()
        sent_ack=True
        report=process.stdout.readline().strip()
        if report!='PR211_SERVER_REPORT_SUBMITTED':
            raise PilotError('Windows got the label, but server ACK is UNKNOWN. NO RETRY')
        print('PR211 WINDOWS RAW SUBMITTED + PG17 SERVER ACK PASS',flush=True)
        print('VERIFY PAPER: WB 59298125228 / GRIT SYNTHETIC / NOT WB',flush=True)
        print('PHYSICAL OUTPUT REQUIRES YOUR CONFIRMATION. DO NOT REPRINT.',flush=True)
        print('Journal:',path,flush=True)
        return 0
    except Exception:
        if not sent_ack and process.poll() is None:
            try:
                process.stdin.write('UNKNOWN\n')
                process.stdin.flush()
            except (BrokenPipeError,OSError,ValueError):
                pass
        raise
    finally:
        try:
            process.stdin.close()
        except Exception:
            pass
        try:
            process.stdout.close()
        except Exception:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main():
    try:
        return execute()
    except Exception as exc:
        print('PR211 PHYSICAL SYNTHETIC TEST FAILED/UNKNOWN:',str(exc),
              file=sys.stderr,flush=True)
        print('DO NOT RETRY IF A CLAIM OR PRINT MAY HAVE STARTED.',
              file=sys.stderr,flush=True)
        return 1


if __name__=='__main__':
    sys.exit(main())
