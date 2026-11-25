"""Use actual GRIT WMS auth inside the *isolated synthetic* HTTP harness.

NO production registration. Importing this module never mounts routes.
Only factory invocation with the isolated-PG gate can create a blueprint.
Employee identity is always returned by wms.get_session_employee() and
manager permission by wms.manager_access(), not by JSON or fixture flags.
"""
import os

from flask import session

from line_printer_pg_trial import _verify_isolated_connection
from line_printer_http_synthetic import make_synthetic_printer_blueprint


def make_real_wms_auth_synthetic_blueprint(connection_factory):
    if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
        raise RuntimeError("Synthetic WMS auth bridge disabled")
    # Verify the actual current_database() before making any HTTP endpoint.
    with connection_factory() as connection:
        _verify_isolated_connection(connection)
    from wms import get_session_employee, manager_access

    def terminal_employee():
        return get_session_employee()

    def manager_authorized():
        # Manager desktop session, never an employee's forged request body.
        return session.get("manager_web") is True and manager_access()

    return make_synthetic_printer_blueprint(
        connection_factory, terminal_employee, manager_authorized)
