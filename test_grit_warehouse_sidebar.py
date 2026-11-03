"""Regression checks for GRIT warehouse sidebar branding."""
from pathlib import Path

def test_manager_page_has_sidebar_art_below_logout():
    src=Path("wms.py").read_text(encoding="utf-8")
    page=src.split('STATS_PAGE = """',1)[1].split('"""',1)[0]
    sidebar=page.split('<aside class="sidebar">',1)[1].split('</aside>',1)[0]
    assert 'Выйти из ПК' in sidebar
    assert 'class="sidebar-art"' in sidebar
    assert sidebar.index('Выйти из ПК') < sidebar.index('class="sidebar-art"')
    assert 'grit-warehouse-sidebar.svg?v=1' in page
    assert 'pointer-events:none' in page
    assert 'overflow-y:auto' in page
    assert '.sidebar-art{display:none}' in page

def test_sidebar_asset_is_real_public_first_party_asset():
    svg=Path("grit_warehouse_sidebar.svg").read_text(encoding="utf-8")
    app=Path("app.py").read_text(encoding="utf-8")
    assert svg.startswith('<svg')
    assert 'fill="#ff9b28"' in svg
    assert '@app.get("/grit-warehouse-sidebar.svg")' in app
    assert 'image_path.read_bytes()' in app
    assert '"/grit-warehouse-sidebar.svg"' in app.split("public_paths = {",1)[1].split("}",1)[0]
