"""Synthetic Flask boundary for QR -> print queue. NOT registered in app.py.

Blueprint uses real GRIT session-bound identity through an injected trusted
authenticated_employee() callback. NEVER accept actor, shift, printer or
order identifiers from the POST body.
"""
import os
import secrets
from flask import Blueprint, jsonify, request, session

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_session_gate import require_bound_terminal_identity
from services.printing.qr_scan import scan_synthetic_printer_qr


def make_synthetic_autoqueue_blueprint(connection_factory, authenticated_employee):
    if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Synthetic printing endpoint is disabled")
    if not callable(authenticated_employee):
        raise RuntimeError("Server-side terminal authentication required")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
    bp = Blueprint("pr192_synthetic_autoqueue", __name__)

    @bp.post("/synthetic/pr192/print-queue/scan")
    def scan_printer_qr():
        identity = require_bound_terminal_identity(
            session, authenticated_employee())
        if not identity:
            return jsonify(error="TERMINAL_SESSION_REQUIRED"), 401
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {"qr", "csrf"}:
            return jsonify(error="ONLY_QR_AND_CSRF_ALLOWED"), 400
        expected = session.get("pr192_print_csrf")
        if (not isinstance(expected, str) or len(expected) < 16 or
                not isinstance(data["csrf"], str) or
                not secrets.compare_digest(data["csrf"], expected)):
            return jsonify(error="CSRF_REQUIRED"), 403
        qr = data["qr"]
        employee_id, shift_id = identity
        result = scan_synthetic_printer_qr(
            connection_factory, qr_payload=qr,
            employee_id=employee_id, shift_id=shift_id)
        if not result.accepted:
            status = 403 if result.reason in (
                "TERMINAL_NOT_ACTIVE", "QR_REVOKED_OR_PRINTER_DISABLED",
                "AUTHORIZATION_DENIED", "INVALID_QR") else 400
            return jsonify(ok=False, error=result.reason), status
        return jsonify(
            ok=True, synthetic=True, physical_printing=False,
            printer_id=result.printer_id,
            queued_new=result.added,
            already_queued=result.already_queued,
            other_printer=result.elsewhere,
            requires_review=result.requires_review)

    return bp


def make_real_wms_synthetic_autoqueue_blueprint(connection_factory):
    """Existing wms.get_session_employee() auth; synthetic PG only."""
    if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Synthetic printing endpoint is disabled")
    with connection_factory() as conn:
        _verify_isolated_connection(conn)
    from wms import get_session_employee
    return make_synthetic_autoqueue_blueprint(
        connection_factory, get_session_employee)
