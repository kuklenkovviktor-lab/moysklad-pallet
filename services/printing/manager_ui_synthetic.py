"""Manager-only virtual printing monitor. Isolated demo ONLY (#192).

No production routes, no USB/physical print, no real WB stickers, and no
device-side operations. The ONLY mutation is an explicit manager-authorized
single virtual sink ACK in a disposable synthetic PostgreSQL database.
"""
from pathlib import Path
import os
import secrets

from flask import Blueprint, Response, jsonify, request, session

from line_printer_pg_trial import _verify_isolated_connection
from services.printing.virtual_agent import deliver_one_virtual


ROOT=Path(__file__).resolve().parents[2]
PAGE=ROOT / "frontend/pages/printing_manager_demo/index.html"
JS=ROOT / "frontend/features/printing_queue/ui.js"
STYLE=ROOT / "frontend/pages/printing_manager_demo/style.css"


def make_synthetic_printing_manager_blueprint(connection_factory, authorized_manager):
    if os.environ.get("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Virtual manager demo disabled outside isolated PG")
    if not callable(authorized_manager):
        raise RuntimeError("Trusted server-side manager guard required")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)

    bp=Blueprint("pr192_virtual_printing_manager",__name__)

    def manager_ok():
        return authorized_manager() is True

    def denied():
        return jsonify(error="MANAGER_REQUIRED"),403,{"Cache-Control":"no-store"}

    @bp.get("/demo/printing")
    def monitor_page():
        if not manager_ok():
            return denied()
        return Response(PAGE.read_text(encoding="utf-8"),
                        mimetype="text/html",
                        headers={"Cache-Control":"no-store",
                                 "X-Content-Type-Options":"nosniff"})

    @bp.get("/synthetic/pr192/assets/printing_queue.js")
    def monitor_script():
        if not manager_ok():
            return denied()
        return Response(JS.read_text(encoding="utf-8"),
                        mimetype="application/javascript",
                        headers={"Cache-Control":"no-store",
                                 "X-Content-Type-Options":"nosniff"})

    @bp.get("/synthetic/pr192/assets/printing_console.css")
    def monitor_style():
        if not manager_ok():
            return denied()
        return Response(STYLE.read_text(encoding="utf-8"),
                        mimetype="text/css",
                        headers={"Cache-Control":"no-store",
                                 "X-Content-Type-Options":"nosniff"})

    @bp.get("/demo/printing/state")
    def printing_state():
        if not manager_ok():
            return denied()
        # Counters cover the entire synthetic queue; tables show last 100 rows.
        # Never use only the paginated rows to calculate global totals.
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            with conn.transaction():
                printers=conn.execute("""SELECT printer_id,name,lines_json,enabled
                    FROM grit_line_printers ORDER BY name,printer_id""").fetchall()
                jobs=conn.execute("""SELECT job_id,order_id,printer_id,
                    state,sticker_kind,version
                    FROM synthetic_pr192_print_jobs
                    WHERE account_id='synthetic'
                    ORDER BY created_at DESC,job_id DESC LIMIT 100""").fetchall()
                events=conn.execute("""SELECT e.job_id,j.order_id,j.printer_id,
                    e.event,e.from_state,e.to_state,e.reason
                    FROM synthetic_pr192_print_events e
                    JOIN synthetic_pr192_print_jobs j ON j.job_id=e.job_id
                    WHERE j.account_id='synthetic'
                    ORDER BY e.event_id DESC LIMIT 100""").fetchall()
                by_state=conn.execute("""SELECT state,count(*)::integer
                    FROM synthetic_pr192_print_jobs
                    WHERE account_id='synthetic'
                    GROUP BY state""").fetchall()
                by_printer=conn.execute("""SELECT printer_id,state,count(*)::integer
                    FROM synthetic_pr192_print_jobs
                    WHERE account_id='synthetic'
                    GROUP BY printer_id,state""").fetchall()
                most_recent=conn.execute("""SELECT DISTINCT ON (printer_id)
                    printer_id,state,order_id
                    FROM synthetic_pr192_print_jobs
                    WHERE account_id='synthetic'
                    ORDER BY printer_id,updated_at DESC,job_id DESC""").fetchall()

        from json import loads
        states=("QUEUED","SENDING","SUBMITTED","PRINTED",
                "FAILED","UNKNOWN","CANCELLED")
        totals={state:0 for state in states}
        totals.update(dict(by_state))
        printer_counts={}
        for printer_id,state,count in by_printer:
            printer_counts.setdefault(printer_id,{}).update({state:count})
        last={printer_id:{"state":state,"order_id":order_id}
              for printer_id,state,order_id in most_recent}
        printer_views=[]
        for printer_id,name,lines_json,enabled in printers:
            counts=printer_counts.get(printer_id,{})
            recent=last.get(printer_id,{})
            printer_views.append({
                "id":printer_id,"name":name,"lines":loads(lines_json),
                "enabled":bool(enabled),
                "queued":counts.get("QUEUED",0),
                "attention":counts.get("UNKNOWN",0)+counts.get("FAILED",0),
                "total":sum(counts.values()),
                "last_state":recent.get("state"),
                "last_order":recent.get("order_id"),
            })
        return jsonify(
            synthetic=True,physical_printing=False,
            summary={**totals,"total":sum(totals.values()),
                     "attention":totals["UNKNOWN"]+totals["FAILED"]},
            printers=printer_views,
            jobs=[{"job_id":j[0],"order_id":j[1],"printer_id":j[2],
                   "state":j[3],"kind":j[4],"version":j[5]} for j in jobs],
            events=[{"job_id":e[0],"order_id":e[1],"printer_id":e[2],
                     "event":e[3],"from":e[4],"to":e[5],"reason":e[6]}
                    for e in events],
            manager_csrf=session.get("pr192_manager_csrf")
        ),200,{"Cache-Control":"no-store"}

    @bp.post("/demo/printing/virtual-deliver")
    def virtual_deliver():
        if not manager_ok():
            return denied()
        data=request.get_json(silent=True)
        if not isinstance(data,dict) or set(data)!={"printer_id","csrf"}:
            return jsonify(error="ONLY_PRINTER_AND_CSRF_ALLOWED"),400
        token=session.get("pr192_manager_csrf")
        csrf=data.get("csrf")
        if (not isinstance(token,str) or len(token)<16 or
                not isinstance(csrf,str) or
                not secrets.compare_digest(token,csrf)):
            return jsonify(error="MANAGER_CSRF_REQUIRED"),403
        printer_id=data["printer_id"]
        if not isinstance(printer_id,str) or not 0<len(printer_id)<=160:
            return jsonify(error="INVALID_PRINTER_ID"),400
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            exists=conn.execute("""SELECT 1 FROM grit_line_printers
                WHERE printer_id=%s""",(printer_id,)).fetchone()
        if not exists:
            return jsonify(error="PRINTER_NOT_REGISTERED"),404

        outcome=deliver_one_virtual(connection_factory,
                                    printer_id=printer_id,
                                    agent_id="synthetic-manager-virtual",
                                    mode="ACK")
        return jsonify(ok=True,synthetic=True,physical_printing=False,
                       job_id=outcome.job_id,state=outcome.state,
                       reason=outcome.reason),200,{"Cache-Control":"no-store"}

    return bp
