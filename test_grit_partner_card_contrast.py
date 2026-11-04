"""Manager partner section dark theme regression checks."""
from pathlib import Path

def test_partner_admin_cards_are_scoped():
    source=Path("partner_pallets.py").read_text(encoding="utf-8")
    theme=Path("grit_theme.py").read_text(encoding="utf-8")
    app=Path("app.py").read_text(encoding="utf-8")
    assert source.count('class="grit-manager-partners-card"') == 2
    assert 'body section.grit-manager-partners-card{' in theme
    assert 'background:linear-gradient(145deg,#16283b,#101c2a)!important' in theme
    assert 'body section.grit-manager-partners-card h2{color:#f1f6ff!important' in theme
    assert 'body section.grit-manager-partners-card form{display:grid;gap:12px}' in theme
    assert '/grit-ui.css?v=2' in app
    assert '"/manager/partners"' in app.split('_GRIT_DESKTOP_PAGES = frozenset({',1)[1].split('})',1)[0]
