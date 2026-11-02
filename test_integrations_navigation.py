"""Regression tests for the manager-only integrations page."""
import pathlib


def test_integrations_manager_redirect_uses_real_login_route():
    integrations = pathlib.Path("integrations.py").read_text(encoding="utf-8")
    wms = pathlib.Path("wms.py").read_text(encoding="utf-8")
    assert 'return redirect("/manager-login")' in integrations
    assert '@wms_bp.get("/manager-login")' in wms
    assert 'redirect("/manager/login")' not in integrations


def test_integrations_does_not_render_secrets():
    integrations = pathlib.Path("integrations.py").read_text(encoding="utf-8")
    assert "secrets_exposed=False" in integrations
    assert "os.getenv(\"MOYSKLAD_TOKEN\", \"\").strip()" in integrations
