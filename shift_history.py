"""Read-only, manager-only history of GRIT WMS shifts and productivity."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, redirect, request

from wms import DATABASE_URL, ensure_db, manager_access, parse_iso, query_all

from shift_history_ui import SHIFT_HISTORY_PAGE

shift_history_bp = Blueprint("shift_history", __name__)
WAREHOUSE_TZ = ZoneInfo("Europe/Moscow")
MAX_DAYS = 31
STALE_SECONDS = 16 * 3600

ACTIVITY_NAMES = {
    "picking": "Сборка FBS",
    "replenishment": "Пополнение",
    "receiving": "Приёмка",
    "shipping": "Отгрузка",
    "pallets": "Палеты",
    "kgt": "КГТ",
    "downtime": "Простой",
    "other": "Другое",
}


def _date_arg(value, fallback):
    if not value:
        return fallback
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Некорректная дата") from exc


def _bounds(args):
    today = datetime.now(WAREHOUSE_TZ).date()
    start_date = _date_arg(args.get("from"), today)
    end_date = _date_arg(args.get("to"), today)
    length = (end_date - start_date).days + 1
    if not 1 <= length <= MAX_DAYS:
        raise ValueError("Выбери период от 1 до 31 дня")
    start = datetime.combine(start_date, time.min, WAREHOUSE_TZ).astimezone(timezone.utc)
    stop = datetime.combine(end_date + timedelta(days=1), time.min, WAREHOUSE_TZ).astimezone(timezone.utc)
    return start_date, end_date, start, stop


def _utc(value):
    dt = parse_iso(value)
    if not dt:
        return None
    if dt.tzinfo is None:
        # All new timestamps use UTC; legacy naive records are treated as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _pieces(start, end):
    """Split one interval at Moscow-local midnight, [start, end)."""
    cursor = start
    while cursor < end:
        local_date = cursor.astimezone(WAREHOUSE_TZ).date()
        midnight = datetime.combine(local_date + timedelta(days=1), time.min, WAREHOUSE_TZ).astimezone(timezone.utc)
        stop = min(midnight, end)
        if stop <= cursor:
            break
        yield local_date.isoformat(), cursor, stop
        cursor = stop


def _scan_day_expr():
    # 'scanned_at' is ISO timestamp text in both storage engines.
    # Moscow is UTC+3 and does not observe seasonal daylight saving.
    if DATABASE_URL:
        return "to_char((scanned_at::timestamptz AT TIME ZONE 'Europe/Moscow'), 'YYYY-MM-DD')"
    return "date(datetime(scanned_at, '+3 hours'))"


def _entry(rows, day, employee_id, names):
    key = (day, employee_id)
    if key not in rows:
        rows[key] = {
            "date": day, "employee_id": employee_id,
            "employee": names.get(employee_id, employee_id),
            "orders": 0, "debt_orders": 0, "picking_seconds": 0,
            "replenishment_seconds": 0, "activity_seconds": {},
            "replenishment_tasks": 0, "replenishment_units": 0,
            "tasks": [], "activities": [], "shifts": [], "warnings": [],
            "time_unreliable": False,
        }
    return rows[key]


def _warn(entry, warning):
    if warning not in entry["warnings"]:
        entry["warnings"].append(warning)


def build_report(start_date, end_date, start_utc, stop_utc, employee_id=""):
    employees = query_all(
        "SELECT id,name,role,active FROM employees ORDER BY name"
    )
    names = {emp[0]: emp[1] for emp in employees}
    if employee_id and employee_id not in names:
        raise ValueError("Сотрудник не найден")
    people = [
        {"id": emp[0], "name": emp[1], "active": bool(emp[3])}
        for emp in employees
    ]
    start_iso, stop_iso = start_utc.isoformat(), stop_utc.isoformat()
    filters = " AND employee_id=?" if employee_id else ""
    args = (start_iso, stop_iso) + ((employee_id,) if employee_id else ())
    data = {}

    # Server-side aggregation keeps large picking history out of application memory.
    day_expr = _scan_day_expr()
    scan_rows = query_all(
        "SELECT employee_id, shift_id, " + day_expr + " AS local_day, "
        "COUNT(*), SUM(CASE WHEN scan_type='debt' THEN 1 ELSE 0 END) "
        "FROM scans WHERE scanned_at>=? AND scanned_at<?" + filters +
        " GROUP BY employee_id,shift_id," + day_expr,
        args,
    )
    scan_by_shift = defaultdict(int)
    for emp, shift_id, day, count, debt in scan_rows:
        entry = _entry(data, day, emp, names)
        entry["orders"] += int(count)
        entry["debt_orders"] += int(debt or 0)
        scan_by_shift[(day, shift_id)] += int(count)

    # Include overlapping shifts and split their activities at midnight.
    shift_filter = " AND s.employee_id=?" if employee_id else ""
    shift_rows = query_all(
        "SELECT s.id,s.employee_id,s.started_at,s.ended_at "
        "FROM shifts s WHERE s.started_at<? "
        "AND (s.ended_at IS NULL OR s.ended_at>?)" + shift_filter +
        " ORDER BY s.started_at",
        (stop_iso, start_iso) + ((employee_id,) if employee_id else ()),
    )
    now_utc = datetime.now(timezone.utc)
    effective_stop = min(now_utc, stop_utc)
    active_shifts = {}
    for shift_id, emp, started, ended in shift_rows:
        shift_start = _utc(started)
        shift_end = _utc(ended) if ended else effective_stop
        if not shift_start or not shift_end or shift_end <= shift_start:
            continue
        capped_start, capped_end = max(shift_start, start_utc), min(shift_end, effective_stop)
        if capped_start >= capped_end:
            continue
        active_shifts[shift_id] = (emp, shift_start, shift_end, bool(ended))
        is_stale = ended is None and (now_utc - shift_start).total_seconds() > STALE_SECONDS
        for day, _, _ in _pieces(capped_start, capped_end):
            entry = _entry(data, day, emp, names)
            entry["shifts"].append({
                "id": shift_id, "started_at": started, "ended_at": ended,
                "orders": scan_by_shift[(day, shift_id)],
                "status": "closed" if ended else "open",
            })
            if is_stale:
                entry["time_unreliable"] = True
                _warn(entry, "Смена открыта более 16 часов: проверь фактическое окончание")

    activity_filter = " AND a.employee_id=?" if employee_id else ""
    activity_rows = query_all(
        "SELECT a.shift_id,a.employee_id,a.activity_type,a.started_at,a.ended_at,"
        "s.ended_at FROM activities a JOIN shifts s ON s.id=a.shift_id "
        "WHERE a.started_at<? AND (a.ended_at IS NULL OR a.ended_at>?)" +
        activity_filter + " ORDER BY a.started_at",
        (stop_iso, start_iso) + ((employee_id,) if employee_id else ()),
    )
    for shift_id, emp, kind, started, ended, shift_ended in activity_rows:
        lo = _utc(started)
        if not lo:
            continue
        hi = _utc(ended) if ended else (_utc(shift_ended) if shift_ended else effective_stop)
        if not hi or hi <= lo:
            continue
        clipped_start = max(lo, start_utc)
        clipped_end = min(hi, effective_stop)
        if clipped_start >= clipped_end:
            continue
        is_stale = ended is None and not shift_ended and (now_utc - lo).total_seconds() > STALE_SECONDS
        for day, begin, finish in _pieces(clipped_start, clipped_end):
            entry = _entry(data, day, emp, names)
            if is_stale:
                entry["time_unreliable"] = True
                _warn(entry, "Открытая операция длится более 16 часов: время исключено из расчёта")
            else:
                seconds = int((finish - begin).total_seconds())
                durations = entry["activity_seconds"]
                durations[kind] = durations.get(kind, 0) + seconds
                if kind == "picking":
                    entry["picking_seconds"] += seconds
                elif kind == "replenishment":
                    entry["replenishment_seconds"] += seconds
                entry["activities"].append({
                    "type": kind, "label": ACTIVITY_NAMES.get(kind, kind),
                    "started_at": begin.isoformat(),
                    "ended_at": finish.isoformat(), "seconds": seconds,
                })

    # Count replenishments by actual completion day and assigned employee,
    # independently from self-reported activity switches.
    task_filter = " AND assigned_employee_id=?" if employee_id else ""
    task_rows = query_all(
        "SELECT assigned_employee_id, completed_at, article,line_code,"
        "completed_qty,started_at FROM replenishment_tasks "
        "WHERE status='completed' AND assigned_employee_id IS NOT NULL "
        "AND completed_at>=? AND completed_at<?" + task_filter +
        " ORDER BY completed_at",
        args,
    )
    for emp, completed, article, location, quantity, started in task_rows:
        complete_dt = _utc(completed)
        if not complete_dt:
            continue
        day = complete_dt.astimezone(WAREHOUSE_TZ).date().isoformat()
        entry = _entry(data, day, emp, names)
        entry["replenishment_tasks"] += 1
        entry["replenishment_units"] += int(quantity or 0)
        entry["tasks"].append({
            "article": article, "location": location, "quantity": int(quantity or 0),
            "started_at": started, "completed_at": completed,
        })

    results = []
    for row in data.values():
        pick_seconds = row["picking_seconds"]
        if row["orders"] and pick_seconds == 0:
            _warn(row, "Нет зафиксированного времени сборки: скорость не рассчитывается")
        row["orders_per_hour"] = (
            round(row["orders"] * 3600 / pick_seconds, 1)
            if pick_seconds > 0 and not row["time_unreliable"] else None
        )
        row["activities"].sort(key=lambda x: x["started_at"])
        row["tasks"].sort(key=lambda x: x["completed_at"])
        results.append(row)
    results.sort(key=lambda x: (x["date"], x["employee"]), reverse=True)

    totals = {
        "orders": sum(x["orders"] for x in results),
        "debt_orders": sum(x["debt_orders"] for x in results),
        "picking_seconds": sum(x["picking_seconds"] for x in results),
        "replenishment_seconds": sum(x["replenishment_seconds"] for x in results),
        "replenishment_tasks": sum(x["replenishment_tasks"] for x in results),
        "replenishment_units": sum(x["replenishment_units"] for x in results),
        "employees": len({x["employee_id"] for x in results}),
        "days": len({x["date"] for x in results}),
        "quality_warning": any(x["warnings"] for x in results),
    }
    totals["orders_per_hour"] = (
        round(totals["orders"] * 3600 / totals["picking_seconds"], 1)
        if totals["picking_seconds"] > 0
        and not any(x["time_unreliable"] for x in results) else None
    )
    return {
        "from": start_date.isoformat(), "to": end_date.isoformat(),
        "timezone": "Europe/Moscow", "employees": people,
        "rows": results, "totals": totals,
    }


@shift_history_bp.get("/manager/shift-history")
def history_page():
    if not manager_access():
        return redirect("/manager-login")
    ensure_db()
    return SHIFT_HISTORY_PAGE


@shift_history_bp.get("/api/manager/shift-history")
def history_api():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    try:
        start_date, end_date, start, stop = _bounds(request.args)
        employee_id = str(request.args.get("employee_id") or "").strip()
        if len(employee_id) > 100:
            raise ValueError("Некорректный идентификатор сотрудника")
        ensure_db()
        return jsonify(build_report(start_date, end_date, start, stop, employee_id))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@shift_history_bp.get("/api/manager/employee-events")
def employee_events_api():
    """Chronological, paginated employee event journal. No writes or guessed metrics."""
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    try:
        _, _, start, stop = _bounds(request.args)
        employee_id = str(request.args.get("employee_id") or "").strip()
        if not employee_id or len(employee_id) > 100:
            raise ValueError("Выбери сотрудника")
        kind = str(request.args.get("kind") or "all").strip()
        if kind not in ("all", "fbs", "operation"):
            raise ValueError("Неизвестный тип операции")
        page = int(request.args.get("page") or "1")
        if not 1 <= page <= 500:
            raise ValueError("Страница должна быть от 1 до 500")
        ensure_db()
        if not query_all("SELECT id FROM employees WHERE id=?", (employee_id,)):
            raise ValueError("Сотрудник не найден")
        start_iso, stop_iso = start.isoformat(), stop.isoformat()
        params = []
        segments = []
        if kind in ("all", "fbs"):
            segments.append(
                "SELECT scanned_at AS occurred_at, 'fbs' AS category, "
                "CASE WHEN scan_type='debt' THEN 'Долг WB FBS' ELSE 'Сборка WB FBS' END AS label, "
                "article,product_name,barcode AS object_code,1 AS quantity,"
                "scan_type AS result, '' AS document_name "
                "FROM scans WHERE employee_id=? AND scanned_at>=? AND scanned_at<?"
            )
            params.extend((employee_id, start_iso, stop_iso))
        if kind in ("all", "operation"):
            segments.append(
                "SELECT created_at AS occurred_at,operation_type AS category,"
                "event_type AS label,article,product_name,object_code,quantity,"
                "result,document_name "
                "FROM operation_events WHERE employee_id=? AND created_at>=? AND created_at<?"
            )
            params.extend((employee_id, start_iso, stop_iso))
        union = " UNION ALL ".join(segments)
        limit = 100
        rows = query_all(
            "SELECT occurred_at,category,label,article,product_name,object_code,"
            "quantity,result,document_name FROM (" + union +
            ") events ORDER BY occurred_at DESC LIMIT ? OFFSET ?",
            tuple(params) + (limit + 1, (page - 1) * limit),
        )
        data = [
            {
                "occurred_at": x[0], "category": x[1], "label": x[2],
                "article": x[3], "product_name": x[4], "object_code": x[5],
                "quantity": x[6], "result": x[7], "document": x[8],
            }
            for x in rows[:limit]
        ]
        return jsonify(rows=data, page=page, has_more=len(rows) > limit, page_size=limit)
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
