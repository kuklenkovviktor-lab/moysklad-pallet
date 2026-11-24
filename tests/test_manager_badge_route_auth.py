"""Regression for manager-only badge print and mutation route dispatch.

No production database, no live badges and no actual printing.
"""
from pathlib import Path

APP=Path("app.py").read_text(encoding="utf-8")
WMS=Path("wms.py").read_text(encoding="utf-8")
SECTION=APP.split("manager_paths = {",1)[1].split("\n    }",1)[0]
DISPATCH=APP.split("# Desktop manager routes are authenticated only",1)[1].split("\n    employee = get_session_employee()",1)[0]


def test_thermal_badge_pages_use_password_manager_session_not_employee_login():
    assert '"/manager/badges/label"' in SECTION
    assert '"/manager/badges/labels/all"' in SECTION
    assert 'if session.get("manager_web") is True:' in DISPATCH
    assert 'return redirect("/manager-login")' in DISPATCH
    assert '@wms_bp.get("/manager/badges/label")' in WMS
    assert '@wms_bp.get("/manager/badges/labels/all")' in WMS


def test_badge_rename_archive_restore_routes_cannot_fall_through_to_employee_auth():
    assert 'path.startswith("/api/admin/badges/")' in DISPATCH
    assert 'return jsonify(error="Требуется вход в ПК-панель"), 403' in DISPATCH
    for method,route in (
        ("patch", "/api/admin/badges/<employee_id>"),
        ("post", "/api/admin/badges/<employee_id>/archive"),
        ("post", "/api/admin/badges/<employee_id>/restore"),
    ):
        assert f'@wms_bp.{method}("{route}")' in WMS


def test_badge_print_still_explicitly_checks_manager_access():
    for fn,next_fn in (
        ("def admin_print_one_badge():","@wms_bp.get(\"/manager/badges/labels/all\")"),
        ("def admin_print_all_badges():","@wms_bp.get(\"/api/admin/badges\")"),
    ):
        block=WMS.split(fn,1)[1].split(next_fn,1)[0]
        assert "if not manager_access():" in block
        assert 'redirect("/manager-login")' in block
    assert "@page {size:58mm 40mm; margin:0}" in WMS


def test_wb_unpicked_days_api_uses_manager_pc_auth_not_employee_badge():
    """New manager endpoints must be registered in the central auth dispatch."""
    assert '"/api/manager/wb-fbs/unpicked/days"' in SECTION
    assert '"/api/manager/wb-fbs/unpicked/groups"' in SECTION
    assert 'if session.get("manager_web") is True:' in DISPATCH
    assert 'return jsonify(error="Требуется вход в ПК-панель"), 403' in DISPATCH
