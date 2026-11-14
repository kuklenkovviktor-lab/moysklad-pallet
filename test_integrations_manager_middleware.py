"""Ensure the desktop manager can open the integrations module without badge login."""
from pathlib import Path


def test_integrations_are_registered_as_desktop_manager_routes():
    app = Path("app.py").read_text(encoding="utf-8")
    manager_section = app.split("manager_paths = {", 1)[1].split("\n    }", 1)[0]
    assert '"/manager/integrations"' in manager_section
    assert '"/api/manager/integrations"' in manager_section
    assert '"/api/manager/integrations/moysklad/check"' in manager_section


def test_integrations_manager_auth_and_redirect_consistent():
    routes = Path("integrations.py").read_text(encoding="utf-8")
    assert '@integrations_bp.get("/manager/integrations")' in routes
    assert '@integrations_bp.get("/api/manager/integrations")' in routes
    assert 'return redirect("/manager-login")' in routes
    assert 'if not manager_access():' in routes
