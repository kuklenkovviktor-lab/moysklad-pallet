"""Card-first employee analytics with manager-only personal event history."""
from pathlib import Path

def test_cards_drilldown_and_existing_report():
    ui=Path("shift_history_ui.py").read_text(encoding="utf-8")
    for token in ('id="cards"','data-employee','function renderCards()',
                  'function openPerson(id)','id="journal"','id="events"',
                  'id="sortBy"','data-kind="all"','data-kind="fbs"',
                  'data-kind="operation"','id="moreEvents"',
                  '/api/manager/shift-history?','/api/manager/employee-events?',
                  'r.picking_seconds','r.replenishment_seconds','r.activities',
                  'r.tasks','r.shifts','r.warnings'):
        assert token in ui
    assert 'эффективность 78%' not in ui

def test_personal_events_is_manager_only_and_paginated():
    backend=Path("shift_history.py").read_text(encoding="utf-8")
    app=Path("app.py").read_text(encoding="utf-8")
    assert '@shift_history_bp.get("/api/manager/employee-events")' in backend
    assert 'if not manager_access():' in backend.split('def employee_events_api():',1)[1]
    assert 'ORDER BY occurred_at DESC LIMIT ? OFFSET ?' in backend
    assert 'limit = 100' in backend
    assert 'if not query_all("SELECT id FROM employees WHERE id=?"' in backend
    assert '"/api/manager/employee-events"' in app.split('manager_paths = {',1)[1].split('}',1)[0]
