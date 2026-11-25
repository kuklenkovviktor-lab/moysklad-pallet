"""Synthetic-only HTTP acceptance harness for printer assignment lifecycle #192.

This Blueprint is NOT registered in app.py. It deliberately only runs against
a PG database named grit_pr192_synthetic_* with the explicit opt-in flag.
In a future live integration, use the real wms.get_session_employee() and
manager_access() at this boundary; the injected functions are TEST FIXTURES.
NO real printer, WB API or production mutations.
"""
import re
import uuid
from flask import Blueprint, jsonify, request, session
from line_printer_session_gate import require_bound_terminal_identity
from line_printer_pg_integrated_trial import validate_locked_synthetic
from line_printer_pg_lifecycle import (
    reserve_lifecycle, complete_lifecycle, release_lifecycle, read_lifecycle,
)
from line_printer_pg_trial import _verify_isolated_connection

_QR_PRINTER = re.compile(r"^GRIT-PRINTER:1:([a-zA-Z0-9_-]{1,40}):")


def make_synthetic_printer_blueprint(connection_factory, authenticated_employee,
                                     manager_is_authorized):
    """Dependency injection keeps synthetic identities out of production code."""
    bp = Blueprint("pr192_http_synthetic", __name__)

    def valid_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None
        # No client-selected employee, shift, manager, account or permissions.
        forbidden = {"employee_id", "shift_id", "actor_id", "manager",
                     "account_id", "is_manager", "role"}
        return None if forbidden.intersection(data) else data

    def valid_id(value):
        return isinstance(value, str) and bool(value.strip()) and len(value) <= 160

    def response(result):
        status = 200 if result.accepted else (
            403 if result.reason in ("AUTHORIZATION_DENIED", "AUTHORIZATION_REQUIRED")
            else 409)
        return jsonify(ok=result.accepted, reason=result.reason, state=result.state), status

    def terminal():
        return require_bound_terminal_identity(session, authenticated_employee())

    def manager():
        # Manager rights are determined ONLY by the server's authenticated
        # manager session, never from JSON body.
        return manager_is_authorized() is True

    @bp.post("/synthetic/pr192/reserve")
    def reserve():
        identity = terminal()
        if not identity:
            return jsonify(error="TERMINAL_SESSION_REQUIRED"), 401
        data = valid_body()
        if not data:
            return jsonify(error="INVALID_BODY"), 400
        order, qr = data.get("order_id"), data.get("printer_qr")
        action_id, reservation_id = data.get("action_id"), data.get("reservation_id")
        if not all(valid_id(v) for v in (order, qr, action_id, reservation_id)):
            return jsonify(error="INVALID_INPUT"), 400
        match = _QR_PRINTER.match(qr)
        if not match:
            return jsonify(error="INVALID_QR"), 400
        employee, shift = identity
        printer_id = match.group(1)
        def authorize(conn, action, previous, row):
            return validate_locked_synthetic(
                conn, account_id="synthetic", order_id=order,
                employee_id=employee, shift_id=shift,
                printer_id=printer_id, qr_payload=qr) == "ELIGIBLE"
        result = reserve_lifecycle(
            connection_factory, account_id="synthetic", order_id=order,
            action_id=action_id, actor_id=employee, reservation_id=reservation_id,
            printer_id=printer_id, employee_id=employee, shift_id=shift,
            authorized_locked=authorize)
        return response(result)

    @bp.post("/synthetic/pr192/complete")
    def complete():
        identity = terminal()
        if not identity:
            return jsonify(error="TERMINAL_SESSION_REQUIRED"), 401
        data = valid_body()
        if not data:
            return jsonify(error="INVALID_BODY"), 400
        order, action_id, lease = (data.get(k) for k in
                                   ("order_id", "action_id", "reservation_id"))
        if not all(valid_id(v) for v in (order, action_id, lease)):
            return jsonify(error="INVALID_INPUT"), 400
        employee, shift = identity
        def authorize(conn, action, previous, row):
            # Completion cannot be requested by an unrelated worker.
            if row is None or row[3] != employee or row[4] != shift:
                return False
            active = conn.execute("""
                SELECT e.active,s.ended_at FROM employees e
                JOIN shifts s ON s.employee_id=e.id
                WHERE e.id=%s AND s.id=%s FOR SHARE OF e,s
            """, (employee, shift)).fetchone()
            if not active or active[0] != 1 or active[1] is not None:
                return False
            picking = conn.execute("""
                SELECT 1 FROM activities WHERE employee_id=%s AND shift_id=%s
                AND activity_type='picking' AND ended_at IS NULL FOR SHARE
            """, (employee, shift)).fetchone()
            if not picking:
                return False
            # Crucial: the ordinary WB scan path must already have completed
            # this order; a mere button click cannot mark it complete.
            order_state = conn.execute("""
                SELECT wb_status,local_status FROM wb_fbs_orders
                WHERE account_id='synthetic' AND order_id=%s FOR UPDATE
            """, (order,)).fetchone()
            return bool(order_state and order_state[0] in ("confirm", "complete")
                        and order_state[1] == "complete")
        result = complete_lifecycle(
            connection_factory, account_id="synthetic", order_id=order,
            action_id=action_id, actor_id=employee, reservation_id=lease,
            authorized_locked=authorize)
        return response(result)

    @bp.post("/synthetic/pr192/release")
    def release():
        if not manager():
            return jsonify(error="MANAGER_REQUIRED"), 403
        data = valid_body()
        if not data:
            return jsonify(error="INVALID_BODY"), 400
        order, action_id, lease, reason = (data.get(k) for k in
            ("order_id", "action_id", "reservation_id", "reason"))
        if not all(valid_id(v) for v in (order, action_id, lease, reason)):
            return jsonify(error="INVALID_INPUT"), 400
        csrf = session.get("pr192_manager_csrf")
        if not valid_id(csrf) or data.get("csrf") != csrf:
            return jsonify(error="CSRF_REQUIRED"), 403
        manager_id = session.get("pr192_manager_id")
        if not valid_id(manager_id):
            return jsonify(error="MANAGER_ID_REQUIRED"), 403
        def authorize(conn, action, previous, row):
            # Prevent release after WB completion, even when lifecycle's
            # previous state still says RESERVED.
            order_state = conn.execute("""
                SELECT local_status FROM wb_fbs_orders
                WHERE account_id='synthetic' AND order_id=%s FOR UPDATE
            """, (order,)).fetchone()
            return bool(order_state and order_state[0] == "pending")
        result = release_lifecycle(
            connection_factory, account_id="synthetic", order_id=order,
            action_id=action_id, actor_id=manager_id,
            reservation_id=lease, reason=reason, authorized_locked=authorize)
        return response(result)

    @bp.get("/synthetic/pr192/status/<order_id>")
    def status(order_id):
        if not manager():
            return jsonify(error="MANAGER_REQUIRED"), 403
        if not valid_id(order_id):
            return jsonify(error="INVALID_ORDER"), 400
        state = read_lifecycle(connection_factory, "synthetic", order_id)
        # Manager can see ownership without exposing QR credentials.
        return jsonify(state=state[0], reservation_id=state[1],
                       printer_id=state[2], employee_id=state[3],
                       shift_id=state[4], version=state[5])

    @bp.get("/synthetic/pr192/audit/<order_id>")
    def audit(order_id):
        if not manager():
            return jsonify(error="MANAGER_REQUIRED"), 403
        if not valid_id(order_id):
            return jsonify(error="INVALID_ORDER"), 400
        with connection_factory() as conn:
            _verify_isolated_connection(conn)
            rows = conn.execute("""
                SELECT action,from_state,to_state,actor_id,reason,reservation_id
                FROM synthetic_line_printer_lifecycle_events
                WHERE account_id='synthetic' AND order_id=%s ORDER BY event_id
            """, (order_id,)).fetchall()
        return jsonify(events=[dict(zip(
            ("action", "from_state", "to_state", "actor_id", "reason",
             "reservation_id"), row)) for row in rows])

    return bp
