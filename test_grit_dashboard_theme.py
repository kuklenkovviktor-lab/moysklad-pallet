"""Static regression tests for GRIT command-center dashboard."""
from pathlib import Path

def _page():
    src = Path("wms.py").read_text(encoding="utf-8")
    return src.split('STATS_PAGE = """', 1)[1].split('"""', 1)[0]

def test_dashboard_keeps_live_data_bindings_and_existing_controls():
    page = _page()
    for needle in (
        'id="todayOrders"', 'id="remaining"', 'id="receivingUnits"',
        'id="deviationCount"', 'id="progressBar"', 'id="hourlyChart"',
        'id="workers"', 'id="completedWorkers"', 'id="deviations"',
        'id="progressDone"', 'id="progressPlan"', 'id="wbAssemblyInfo"',
        'href="/manager/attendance/qr"',
        'wbAssemblyCount.remaining_orders', 'wbAssemblyCount.assembled_assembly_orders',
        'wbAssemblyCount.assembly_orders',
        '/api/fbs/dashboard?', '/api/receiving/overview?',
        '/api/manager/wb-fbs/picking-queue', '/api/operations/deviations?',
    ):
        assert needle in page

def test_dashboard_theme_and_links():
    page = _page()
    for needle in (
        'Операционный центр склада', 'class="card processes"',
        'class="process-grid"', 'class="quick-grid"',
        'class="card hourly-card"', 'class="card deviations-card"',
        '--grit-orange:#ff922e', '--grit-blue:#238fe0',
        'href="/manager/warehouse-map"', 'href="/manager/shift-history"',
    ):
        assert needle in page
    assert '9 840' not in page
    assert '47 320' not in page
