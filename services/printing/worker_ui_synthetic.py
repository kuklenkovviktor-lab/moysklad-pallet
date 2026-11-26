"""Isolated worker scanner demo: serves FSD UI, never the production /fbs.

This blueprint is only registered by the dedicated localhost synthetic demo.
It uses the existing GRIT authenticated_employee() callback and refuses any
database other than the explicit disposable PostgreSQL17 test namespace.
"""
import os
from pathlib import Path

from flask import Blueprint, Response, jsonify, session
from line_printer_pg_trial import _verify_isolated_connection
from line_printer_session_gate import require_bound_terminal_identity


ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "frontend/pages/printer_scan_demo/index.html"
FEATURE = ROOT / "frontend/features/printer_scan/scan.js"


def make_synthetic_worker_scanner_blueprint(connection_factory, authenticated_employee):
    if os.environ.get("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Isolated scanner demo is disabled")
    if not callable(authenticated_employee):
        raise RuntimeError("WMS terminal auth required")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)

    bp = Blueprint("pr192_worker_scanner_demo", __name__)

    def worker():
        return require_bound_terminal_identity(session, authenticated_employee())

    @bp.get("/demo/worker-qr")
    def worker_page():
        if not worker():
            return jsonify(error="TERMINAL_SESSION_REQUIRED"), 401
        return Response(
            PAGE.read_text(encoding="utf-8"),
            mimetype="text/html",
            headers={"Cache-Control":"no-store",
                     "X-Content-Type-Options":"nosniff"})

    @bp.get("/synthetic/pr192/assets/printer_scan.js")
    def worker_feature_asset():
        if not worker():
            return jsonify(error="TERMINAL_SESSION_REQUIRED"), 401
        return Response(
            FEATURE.read_text(encoding="utf-8"),
            mimetype="application/javascript",
            headers={"Cache-Control":"no-store",
                     "X-Content-Type-Options":"nosniff"})

    return bp
