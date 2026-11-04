"""Regression checks for the GRIT employee card and event journal UX."""
from pathlib import Path
import shutil
import subprocess


def _page():
    return Path("shift_history_ui.py").read_text(encoding="utf-8")


def test_card_visuals_and_real_statuses():
    page = _page()
    assert 'class="avatar"><svg viewBox="0 0 24 24"' in page
    assert 'Есть операции за период' in page
    assert 'Нет операций за период' in page
    assert 'Учётная запись отключена' in page
    assert "has-operations" in page
    assert '♙' not in page
    assert '@media(prefers-reduced-motion:reduce)' in page
    assert "p.t.orders" not in page or 'num(t.orders)' in page


def test_old_employee_events_cannot_overwrite_current_selection():
    page = _page()
    assert 'function cancelEventRequest()' in page
    assert 'eventController.abort()' in page
    assert 'signal:controller.signal' in page
    assert 'requestId!==eventRequestId||selectedId!==employeeId||eventKind!==kind' in page
    assert "if(reset)cancelEventRequest();" in page
    assert "if(requestId===eventRequestId)" in page
    assert "e.name==='AbortError'" in page
    assert "cancelEventRequest();$('journal').classList.add('hidden')" in page


def test_javascript_parses_with_node_when_installed():
    if not shutil.which("node"):
        return
    page = _page()
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    checked = subprocess.run(
        ["node", "--check", "--input-type=commonjs"],
        input=script, text=True, capture_output=True, timeout=15,
    )
    assert checked.returncode == 0, checked.stderr
