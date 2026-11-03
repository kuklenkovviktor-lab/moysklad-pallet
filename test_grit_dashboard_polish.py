"""Visual and functional invariants of the GRIT manager dashboard."""
from pathlib import Path

def dashboard():
    src=Path("wms.py").read_text(encoding="utf-8")
    return src.split('STATS_PAGE = """',1)[1].split('"""',1)[0]

def test_professional_svg_navigation():
    page=dashboard()
    assert page.count('<symbol id="ic-') >= 20
    assert 'class="icon-defs"' in page
    for icon in ("home","calendar","barcode","download","map","alert","qr"):
        assert 'href="#ic-'+icon+'"' in page
    for emoji in ("🖥️ Главная","📥 Приёмка","📦 Пополнение","🔐 QR выхода"):
        assert emoji not in page

def test_empty_states_and_nonfictional_data():
    page=dashboard()
    assert 'Нет данных за сегодня' in page
    assert 'Сборка заказов ещё не началась' in page
    assert 'Нет активных смен' in page
    assert 'Отклонений сегодня нет' in page
    assert "if(!total)" in page
    assert "box.classList.remove('is-empty')" in page
    assert 'renderHourly(hour.rows||[])' in page
    for fake in ("47 320","9 840","5 695"):
        assert fake not in page

def test_existing_warehouse_operations_remain_connected():
    page=dashboard()
    for token in (
        'id="todayOrders"', 'id="remaining"', 'id="receivingUnits"',
        'id="deviationCount"', 'id="hourlyChart"', 'id="progressBar"',
        'id="workers"', 'id="completedWorkers"', 'id="deviations"',
        'id="planInput"', 'onclick="savePlan()"', 'onclick="openExitQr()',
        'onclick="managerLogout()', 'function renderEmployeeShift',
        'function renderProgress', 'function renderHourly',
        "fetchWithTimeout('/api/fbs/dashboard?",
        "fetchWithTimeout('/api/receiving/overview?",
        "fetchWithTimeout('/api/fbs/hourly?",
        'function refreshExitQr()', 'function syncWb()'
    ):
        assert token in page

def test_dash_does_not_double_apply_other_theme():
    app=Path("app.py").read_text(encoding="utf-8")
    section=app.split('_GRIT_DESKTOP_PAGES = frozenset({',1)[1].split('})',1)[0]
    assert '"/manager"' not in section
    assert '"/manager/shifts"' in section
