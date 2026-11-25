"""Pure HTTP-boundary identity checks for future GRIT printer routes (#192).

Do not use request JSON to choose an employee or shift. The caller must first
run GRIT's get_session_employee() to check employee and shift database liveness.
No endpoint is registered here; only a reusable fail-closed precondition.
"""


def require_bound_terminal_identity(browser_session, authenticated_employee):
    """Return (employee_id, shift_id) from GRIT's verified session or refuse.

    authenticated_employee must come from wms.get_session_employee(), never
    from a scanner payload or trusted client parameter. DB liveness is checked
    again in the PostgreSQL claim transaction. This is NOT authorization by
    itself and cannot permit printing or claim replay without the DB checks.
    """
    if not isinstance(authenticated_employee, dict):
        return None
    if browser_session.get("manager_web") is True:
        return None
    if browser_session.get("attendance_verified") is not True:
        return None
    employee_id = authenticated_employee.get("id")
    cookie_employee = browser_session.get("employee_id")
    shift_id = browser_session.get("employee_shift_id")
    if not all(isinstance(v, str) and 0 < len(v.strip()) <= 160
               for v in (employee_id, cookie_employee, shift_id)):
        return None
    if employee_id != cookie_employee:
        return None
    return employee_id, shift_id
