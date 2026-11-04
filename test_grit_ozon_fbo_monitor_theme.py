"""Visual regression guards for GRIT Ozon FBO manager monitor."""
from pathlib import Path

def test_monitor_is_dark_and_worker_scanning_is_untouched():
    source=Path("ozon_fbo_picking_ui.py").read_text(encoding="utf-8")
    worker=source.split('WORKER_PICK_HTML = """',1)[1].split('"""',1)[0]
    manager=source.split('MANAGER_PICK_HTML = """',1)[1].split('"""',1)[0]
    assert 'Монитор сборки Ozon FBO' in manager
    assert 'background:var(--bg)' in manager
    assert '--bg:#09121e' in manager
    assert 'class="card monitor-card"' in manager
    assert 'class="card audit-card"' in manager
    assert 'class="status-badge ' in manager
    assert "data-history" in manager and "data-release" in manager
    assert '/api/manager/ozon-fbo/progress' in manager
    assert '/api/manager/ozon-fbo/audit/' in manager
    assert '/api/manager/ozon-fbo/release' in manager
    assert 'setInterval(' in manager and '15000' in manager
    assert 'background:#e9eef3' in worker
    assert 'id="barcode"' in worker
    assert 'id="claimBtn"' in worker
