"""GRIT isolated Windows USB bridge: one manager-approved synthetic label per run.

NEVER auto-runs, polls, retries or sends WB sticker bytes. HTTP only to
127.0.0.1:18769 over SSH. The single-claim
server marks the job SENDING before handing a ticket to this agent.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

if __package__:
    from . import usb_agent_config
    from .grit_usb_print_pilot import (
        TEST_PRINTER, PilotError, _spooler, check_printer,
        _send_exactly_one, _write_once, _update_journal, _utc_now,
    )
else:
    import usb_agent_config
    from grit_usb_print_pilot import (
        TEST_PRINTER, PilotError, _spooler, check_printer,
        _send_exactly_one, _write_once, _update_journal, _utc_now,
    )

PORT = 18769
BASE_URL = "http://127.0.0.1:18769"
CONFIRM = "GRIT-BRIDGE-ONE-SYNTHETIC-LABEL"


def build_ticket_label(ticket_id):
    """Build a safe fixed TSPL label locally: NEVER accept TSPL from server."""
    if not isinstance(ticket_id,str) or not re.fullmatch("[0-9a-f]{32}",ticket_id):
        raise PilotError("Invalid synthetic claim ticket")
    short = ticket_id[:16]
    lines = (
        "SIZE 58 mm,40 mm",
        "GAP 2 mm,0 mm",
        "DIRECTION 1",
        "CLS",
        "BOX 12,12,452,308,2",
        'TEXT 20,24,"3",0,1,1,"GRIT WMS"',
        'TEXT 20,62,"2",0,1,1,"USB DEMO ONLY"',
        'TEXT 20,94,"2",0,1,1,"NOT WB"',
        'TEXT 20,136,"1",0,1,1,"TICKET '+short[:8]+'"',
        'TEXT 20,268,"2",0,1,1,"SYNTHETIC 1/1"',
        'QRCODE 240,42,L,5,A,0,"GRIT-DEMO-'+short+'"',
        "PRINT 1,1",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_api_call(token):
    if not isinstance(token,str) or not 32<=len(token)<=256:
        raise PilotError("Invalid isolated credential; run --enroll")
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),_NoRedirect)

    def api(method, path, body=None):
        if method not in ("GET","POST") or path not in (
            "/synthetic/pr192/usb-agent/ping",
            "/synthetic/pr192/usb-agent/claim",
            "/synthetic/pr192/usb-agent/report",
        ):
            raise PilotError("Only allowlisted loopback endpoints are permitted")
        if method=="GET" and body is not None:
            raise PilotError("GET request cannot contain data")
        payload=(json.dumps(body,separators=(",",":")).encode("utf-8")
                 if method=="POST" else None)
        req=urllib.request.Request(
            BASE_URL+path,data=payload,method=method,
            headers={"X-GRIT-USB-Agent-Token":token,
                     "Content-Type":"application/json","Accept":"application/json"})
        try:
            with opener.open(req,timeout=6) as res:
                if not res.headers.get("Content-Type","").startswith("application/json"):
                    raise PilotError("Unexpected bridge response type")
                raw=res.read(8193)
        except (urllib.error.HTTPError,urllib.error.URLError,
                TimeoutError,OSError) as exc:
            raise PilotError("Synthetic bridge is unavailable/denied; check SSH tunnel and token") from exc
        if len(raw)>8192:
            raise PilotError("Bridge response too large")
        try:
            data=json.loads(raw)
        except (ValueError,UnicodeError) as exc:
            raise PilotError("Invalid bridge JSON response") from exc
        if not isinstance(data,dict) or data.get("synthetic") is not True or data.get("physical_printing") is not False:
            raise PilotError("Refusing non-synthetic bridge response")
        return data
    return api


def _journal_path(ticket_id,state_dir=None):
    if state_dir is None:
        base=os.environ.get("LOCALAPPDATA","")
        if not base:
            raise PilotError("LOCALAPPDATA is required for permanent no-retry journal")
        state_dir=Path(base)/"GRIT-WMS"/"USB-Bridge"
    return Path(state_dir)/(ticket_id+".json")


def run_once(api, spooler, *, state_dir=None, automatic_stand=None):
    """One explicit approved claim -> local journal -> one RAW job -> report.

    After ANY uncertain event, never take the same ticket for another print.
    A server-side claim has no lease auto-expiration: no automatic retry.
    """
    port=check_printer(spooler,TEST_PRINTER)  # BEFORE remotely claiming
    ping=api("GET","/synthetic/pr192/usb-agent/ping")
    if ping.get("device")!=TEST_PRINTER:
        raise PilotError("Bridge does not match allowlisted Windows printer")
    claim_body = ({"auto": True, "stand_id": automatic_stand}
                  if automatic_stand is not None else {})
    claimed=api("POST","/synthetic/pr192/usb-agent/claim",claim_body)
    if claimed.get("state")=="EMPTY":
        return "EMPTY",None
    if claimed.get("state")=="REJECTED":
        return "REJECTED",None
    ticket=claimed.get("ticket_id")
    if (claimed.get("state")!="CLAIMED" or
        claimed.get("windows_printer")!=TEST_PRINTER):
        raise PilotError("Unexpected bridge claim: no print")
    payload=build_ticket_label(ticket)
    path=_journal_path(ticket,state_dir)
    record={"ticket_id":ticket,"state":"CLAIMED",
            "printer":TEST_PRINTER,"usb_port":port,
            "synthetic":True,"physical_print_confirmed":False,
            "label_sha256":hashlib.sha256(payload).hexdigest(),
            "created_utc":_utc_now()}
    # An existing journal MUST NOT be modified or marked UNKNOWN on replay.
    # No physical spooler call has happened yet for this invocation.
    _write_once(path,record)
    spool_job_id=None
    try:
        check_printer(spooler,TEST_PRINTER)  # re-check after server claim
        spool_job_id=_send_exactly_one(spooler,TEST_PRINTER,payload)
        _update_journal(path,record,"SUBMITTED_TO_WINDOWS",
                        spool_job_id=spool_job_id)
    except Exception as exc:
        if path.exists():
            try:
                _update_journal(path,record,"UNKNOWN")
            except Exception:
                pass
        try:
            api("POST","/synthetic/pr192/usb-agent/report",
                {"ticket_id":ticket,"result":"UNKNOWN","spool_job_id":None})
        except Exception:
            pass  # server will keep SENDING; NEVER auto-retry
        raise PilotError("USB send outcome UNKNOWN; do not reprint/reclaim") from exc
    try:
        answer=api("POST","/synthetic/pr192/usb-agent/report",
            {"ticket_id":ticket,"result":"SUBMITTED_TO_WINDOWS",
             "spool_job_id":spool_job_id})
        if answer.get("state")!="SUBMITTED":
            raise PilotError("Report not acknowledged")
    except Exception as exc:
        raise PilotError(
            "Spooler received job but bridge ACK is uncertain; DO NOT PRINT AGAIN"
        ) from exc
    return "SUBMITTED_TO_WINDOWS",path


def main(argv=None, *,api=None,spooler=None,state_dir=None):
    parser=argparse.ArgumentParser(description="GRIT isolated USB demo, never WB")
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--enroll",action="store_true",help="Bind from protected HOSTKEY file over SSH, no printing")
    mode.add_argument("--ping",action="store_true",
                      help="Check localhost SSH tunnel and synthetic device token")
    mode.add_argument("--check",action="store_true",
                      help="Check tunnel + dedicated USB printer without printing")
    mode.add_argument("--once",action="store_true",
                      help="Manually claim and print ONE preapproved synthetic job")
    mode.add_argument("--auto",action="store_true",
                      help="Explicit bounded auto mode: ONLY QR-authorized synthetic tickets")
    mode.add_argument("--auto-one",action="store_true",
                      help="One and only one QR-approved synthetic 58x40 label, then exit")
    parser.add_argument("--confirm",default="",
                        help="Required only for one physical job")
    args=parser.parse_args(argv)
    stack=ExitStack()
    try:
        if (args.once or args.auto or args.auto_one) and args.confirm!=CONFIRM:
            raise PilotError("Physical command requires --confirm "+CONFIRM)
        if api is None:
            stack.enter_context(usb_agent_config.tunnel())
        if args.enroll:
            config=usb_agent_config.enrollment_from_ssh()
            bound=usb_agent_config.bind_api(make_api_call(config["token"]),config)
            bound("GET","/synthetic/pr192/usb-agent/ping")
            check_printer(spooler if spooler is not None else _spooler(),TEST_PRINTER)
            usb_agent_config.save(config)
            print("READY: current-user protected binding saved; NO PRINT")
            return 0
        if api is None:
            config=usb_agent_config.load()
            device_api=usb_agent_config.bind_api(make_api_call(config["token"]),config)
        else:
            device_api=api
        if args.ping:
            data=device_api("GET","/synthetic/pr192/usb-agent/ping")
            print("SYNTHETIC BRIDGE PING PASS; approvals:",data.get("waiting"))
            return 0
        device=spooler if spooler is not None else _spooler()
        port=check_printer(device,TEST_PRINTER)
        print("LOCAL USB CHECK PASS:",TEST_PRINTER,"port:",port)
        if args.check:
            if api is None:
                # On explicitly armed automatic demo, Check validates the
                # device/stand rather than stale single-ticket enrollment.
                # Check is always NO PRINT, even if jobs are waiting.
                probe=make_api_call(config["token"])
                data=probe("GET","/synthetic/pr192/usb-agent/ping")
                if data.get("automatic_synthetic") is True:
                    if (data.get("stand_id")!=config["stand_id"]
                        or data.get("device")!=TEST_PRINTER):
                        raise PilotError("Wrong isolated stand/printer")
                else:
                    data=device_api("GET","/synthetic/pr192/usb-agent/ping")
            else:
                data=device_api("GET","/synthetic/pr192/usb-agent/ping")
            if data.get("device")!=TEST_PRINTER:
                raise PilotError("Remote device mismatch")
            print("READY; BRIDGE DRY RUN PASS; waiting:",data.get("waiting"),
                  "; NO PRINT")
            return 0
        if args.auto or args.auto_one:
            if api is not None:
                raise PilotError("Automatic mode requires locally bound secure SSH config")
            # The saved per-test ticket is NOT authorization for automatic mode.
            # Trust only the configured synthetic host/device token, isolated
            # stand ID and explicitly armed server-side AUTO flag.
            raw_api=make_api_call(config["token"])
            ping=raw_api("GET","/synthetic/pr192/usb-agent/ping")
            if (ping.get("stand_id")!=config["stand_id"]
                or ping.get("device")!=TEST_PRINTER
                or ping.get("automatic_synthetic") is not True):
                raise PilotError("Isolated automatic USB mode not armed or wrong stand")
            submitted=0
            deadline=time.monotonic()+300
            after_last=None
            max_jobs = 1 if args.auto_one else 16
            while submitted < max_jobs and time.monotonic() < deadline:
                # A new SSH tunnel is NOT opened per job.
                result,journal=run_once(raw_api,device,state_dir=state_dir,
                                        automatic_stand=config["stand_id"])
                if result == "EMPTY":
                    if args.auto_one:
                        raise PilotError("No one-shot synthetic approval; NO PRINT")
                    if after_last is not None and time.monotonic()-after_last>=12:
                        break
                    time.sleep(2)
                    continue
                if result!="SUBMITTED_TO_WINDOWS":
                    raise PilotError("Automatic USB claim rejected; do not retry")
                submitted+=1
                after_last=time.monotonic()
                print("AUTO USB SYNTHETIC SUBMITTED:",submitted,
                      "(Windows spool ACK only, not physically verified)",flush=True)
            print("AUTO USB SYNTHETIC BATCH COMPLETE:",submitted,
                  "NO RETRY; NOT WB; NOT PHYSICAL CONFIRMATION")
            return 0
        result,journal=run_once(device_api,device,state_dir=state_dir)
        if result != "SUBMITTED_TO_WINDOWS":
            raise PilotError("No eligible approval; no label submitted")
        print("USB BRIDGE RESULT:",result)
        if journal:
            print("Journal:",journal)
            print("SUBMITTED means Windows spooler ACK; NOT physical PRINTED.")
            print("Never rerun --once to fix an uncertain result.")
        return 0
    except (PilotError,OSError,ValueError,ImportError) as exc:
        uncertain = "UNKNOWN" in str(exc) or "ACK is uncertain" in str(exc)
        print("UNKNOWN" if uncertain else "FAILED", ": GRIT USB BRIDGE REFUSED:",exc,file=sys.stderr)
        return 1
    finally:
        stack.close()


if __name__=="__main__":
    raise SystemExit(main())
