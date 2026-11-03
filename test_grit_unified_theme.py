"""Regression checks for the shared manager UI theme.

Theme must never change the scanning/label-print surfaces or authentication policy.
"""
from pathlib import Path

def test_desktop_page_allowlist_and_public_stylesheet():
    app = Path("app.py").read_text(encoding="utf-8")
    routes = app.split("_GRIT_DESKTOP_PAGES = frozenset({",1)[1].split("})",1)[0]
    for p in ("/fbs-journal", "/receiving-monitor", "/history",
              "/manager/replenishment", "/manager/warehouse-map",
              "/manager/cell-audit", "/manager/fbs-diagnostics",
              "/manager/shift-history", "/manager/fbs-pdf", "/manager/ozon-fbo",
              "/manager/integrations", "/manager/partners"):
        assert '"'+p+'"' in routes
    for excluded in ("/manager", "/login", "/replenishment", "/partners/uploads",
                     "/partners/shipments", "/pwa-icon-192.png", "/manager-login"):
        assert '"'+excluded+'"' not in routes
    assert '"/grit-ui.css"' in app.split("public_paths = {",1)[1].split("}",1)[0]
    assert '@app.after_request' in app
    assert 'response.set_data(html)' in app

def test_theme_retains_critical_accessibility_and_print_contrast():
    css = Path("grit_theme.py").read_text(encoding="utf-8")
    assert "--g-orange:#ff9230" in css
    assert "--g-blue:#2d92e2" in css
    assert "focus-visible" in css
    assert "@media print" in css
    assert ".badge *" in css
    assert "body svg{max-width:100%}" in css

def test_brand_titles_updated_without_changing_scanner_logic():
    for name in ("replenishment.py", "warehouse_map.py", "cell_audit.py",
                 "fbs_diagnostics.py", "ozon_fbo.py"):
        src=Path(name).read_text(encoding="utf-8")
        assert "<title>ONIX ·" not in src
        assert "<title>ONIX WMS ·" not in src
