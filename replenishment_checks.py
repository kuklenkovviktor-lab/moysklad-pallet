"""Replenishment-only catalog lookup and scan verification. Does not touch FBS/receiving."""
import html
import uuid

from flask import Blueprint, jsonify, redirect, request
from wms import db_connect, ensure_db, get_session_employee, manager_access, now_iso, query_all, query_one, sql

replenishment_checks_bp = Blueprint("replenishment_checks", __name__)


def _scan_code(value):
    code = str(value or "").strip()
    if len(code) >= 3 and code[0] == "]" and code[1].isalpha() and code[2].isdigit():
        code = code[3:]
    return code.replace("\n", "").replace("\r", "").replace("\t", "").strip()


def _audit(cur, task_id, employee, event, code):
    cur.execute(sql("""
        INSERT INTO replenishment_task_events
        (id, task_id, actor_id, actor, kind, note, quantity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """), (str(uuid.uuid4()), task_id, employee["id"], employee["name"],
          event, code, None, now_iso()))


@replenishment_checks_bp.get("/api/manager/replenishment/catalog")
def replenishment_catalog():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    q = str(request.args.get("q") or "").strip().casefold()
    if len(q) < 2:
        return jsonify(products=[])
    try:
        # Uses the same product catalog/cache as existing MoySklad lookup,
        # without modifying the receiving/FBS scanners.
        from app import _get_product_list, _barcode_values, find_product_by_any_barcode
        matches = []
        for p in _get_product_list():
            article = str(p.get("article") or "").strip()
            name = str(p.get("name") or "").strip()
            code = str(p.get("code") or "").strip()
            if not p.get("id") or not article:
                continue
            barcodes = _barcode_values(p.get("barcodes"))
            for pack in p.get("packs") or []:
                barcodes.extend(_barcode_values(pack.get("barcodes")))
                barcodes.extend(_barcode_values(pack.get("barcode")))
            searched = [article, name, code] + barcodes
            if any(q in str(value).casefold() for value in searched):
                matches.append({"id": p["id"], "article": article, "name": name, "code": code})
            if len(matches) >= 35:
                break
        # Some MoySklad list entries omit pack barcodes until detailed product load.
        if not matches and len(q) >= 7:
            product, _pack = find_product_by_any_barcode(q)
            if product and product.get("id") and product.get("article"):
                matches.append({
                    "id": product["id"], "article": product["article"],
                    "name": product.get("name") or "", "code": product.get("code") or "",
                })
        return jsonify(products=matches)
    except Exception:
        return jsonify(error="Не удалось получить номенклатуру МойСклад; попробуй позже"), 502


@replenishment_checks_bp.get("/api/manager/replenishment/locations")
def replenishment_locations():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    rows = query_all("""
        SELECT id, label, barcode, created_at FROM replenishment_locations
        WHERE active = 1 ORDER BY label LIMIT 1000
    """)
    return jsonify(locations=[
        {"id": x[0], "label": x[1], "barcode": x[2], "created_at": x[3]}
        for x in rows
    ])


@replenishment_checks_bp.post("/api/manager/replenishment/locations")
def replenishment_location_create():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not request.is_json:
        return jsonify(error="Ожидается JSON"), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Неверные данные"), 400
    label = str(data.get("label") or "").strip().upper()
    if not 1 <= len(label) <= 80 or any(ord(c) < 32 for c in label):
        return jsonify(error="Введите название ячейки длиной от 1 до 80 символов"), 400
    existing = query_one("SELECT id FROM replenishment_locations WHERE label = ?", (label,))
    if existing:
        return jsonify(error="Такая ячейка уже зарегистрирована"), 409
    location_id = str(uuid.uuid4())
    barcode = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql("""
                    INSERT INTO replenishment_locations (id, label, barcode, created_at)
                    VALUES (?, ?, ?, ?)
                """), (location_id, label, barcode, now_iso()))
                conn.commit()
            finally:
                cur.close()
    except Exception:
        return jsonify(error="Не удалось создать ячейку; проверь, не существует ли она уже"), 409
    return jsonify(ok=True, location={"id": location_id, "label": label, "barcode": barcode}), 201


@replenishment_checks_bp.get("/manager/replenishment/label")
def replenishment_print_label():
    """58 × 40 mm thermal label: one QR (original code) plus small cell address."""
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    location_id = str(request.args.get("id") or "").strip()
    row = query_one(
        "SELECT label, barcode FROM replenishment_locations WHERE id = ? AND active = 1",
        (location_id,),
    )
    if not row:
        return "Ячейка не найдена", 404

    # Preserve the exact previously generated ONIX-LOC barcode value. Existing
    # verification routes accept this value independently of its symbology.
    import base64
    import io
    import qrcode

    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=16,
        border=4,  # 4-module quiet zone required for reliable QR reading
    )
    qr.add_data(row[1])
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    qr_image = base64.b64encode(buffer.getvalue()).decode("ascii")

    label = html.escape(str(row[0]))
    return """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ONIX · QR ячейки · 58×40 мм</title>
<style>
@page {size:58mm 40mm; margin:0}
* {box-sizing:border-box}
html,body {margin:0;padding:0;font-family:Arial,Helvetica,sans-serif}
body {background:#edf1f4}
.tools {padding:15px;text-align:center;font-size:13px;color:#334}
.tools button {font:inherit;padding:10px 18px;cursor:pointer}
.sheet {width:58mm;height:40mm;margin:12px auto;background:white;
        display:flex;flex-direction:column;align-items:center;
        justify-content:center;overflow:hidden}
.qr {display:block;width:30mm;height:30mm;object-fit:contain;
     image-rendering:pixelated;flex-shrink:0}
.address {font-size:12pt;font-weight:700;line-height:5mm;
          max-width:54mm;white-space:nowrap;text-overflow:ellipsis;
          overflow:hidden;text-align:center;color:#000}
@media print {
  html,body {width:58mm;height:40mm;background:white;overflow:hidden}
  .tools {display:none!important}
  .sheet {margin:0;width:58mm;height:40mm;break-inside:avoid}
}
</style>
</head><body>
<div class="tools"><button type="button" onclick="window.print()">Печать · 58×40 мм</button>
<div>В настройках принтера выбери 58×40 мм, масштаб 100%, поля 0, без колонтитулов.</div></div>
<div class="sheet"><img class="qr" alt="QR-код ячейки" src="data:image/png;base64,""" + qr_image + """"/>
<div class="address">""" + label + """</div></div>
</body></html>"""

@replenishment_checks_bp.post("/api/replenishment/verify")
def replenishment_verify():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Требуется вход по бейджу"), 401
    if not request.is_json:
        return jsonify(error="Ожидается JSON"), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Неверный запрос"), 400
    task_id = str(data.get("task_id") or "").strip()
    stage = str(data.get("stage") or "").strip()
    code = _scan_code(data.get("barcode"))
    if not task_id or stage not in ("product", "location") or not 1 <= len(code) <= 160:
        return jsonify(error="Нужен ШК товара или ячейки"), 400

    task = query_one("""
        SELECT status, assigned_employee_id, verify_required, product_id, article,
               line_code, verified_product_at, verified_location_at
        FROM replenishment_tasks WHERE id = ?
    """, (task_id,))
    if not task or task[0] != "started" or task[1] != emp["id"]:
        return jsonify(error="Сначала прими задачу под своим бейджем"), 409
    if not task[2]:
        return jsonify(error="У этой старой задачи не включена двойная проверка"), 409

    if stage == "product":
        if task[6]:
            return jsonify(error="Товар уже проверен по этой задаче"), 409
        if code.startswith("ONIX-LOC-"):
            return jsonify(error="Считан код ячейки, требуется штрихкод товара"), 409
        try:
            from app import find_product_by_any_barcode
            product, pack = find_product_by_any_barcode(code)
        except Exception:
            return jsonify(error="Ошибка проверки ШК по МойСклад. Не продолжай без подтверждения."), 502
        if not product:
            return jsonify(error="Товар с таким штрихкодом не найден в МойСклад"), 404
        product_id = str(product.get("id") or "")
        article = str(product.get("article") or "").strip()
        if (task[3] and product_id != task[3]) or article.casefold() != str(task[4]).strip().casefold():
            return jsonify(
                error="НЕ ТОТ ТОВАР: ожидается " + str(task[4]) + ", отсканирован " + (article or "без артикула"),
                expected_article=task[4], actual_article=article,
            ), 409
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql("""
                    UPDATE replenishment_tasks
                    SET verified_product_barcode = ?, verified_product_at = ?
                    WHERE id = ? AND status = 'started' AND assigned_employee_id = ?
                      AND verify_required = 1 AND verified_product_at IS NULL
                """), (code, now_iso(), task_id, emp["id"]))
                if cur.rowcount != 1:
                    return jsonify(error="Задача уже изменена. Обнови экран."), 409
                _audit(cur, task_id, emp, "product_verified", code)
                conn.commit()
            finally:
                cur.close()
        return jsonify(ok=True, stage="product", article=article, name=product.get("name") or "",
                       barcode=code, pack_name=(pack or {}).get("name") or "")

    if not task[6]:
        return jsonify(error="Сначала отсканируй и подтверди товар"), 409
    if task[7]:
        return jsonify(error="Ячейка уже подтверждена"), 409
    location = query_one(
        "SELECT label FROM replenishment_locations WHERE barcode = ? AND active = 1",
        (code,),
    )
    if not location:
        return jsonify(error="Неизвестная ячейка. Используй этикетку ONIX, напечатанную руководителем."), 404
    if str(location[0]).strip().casefold() != str(task[5]).strip().casefold():
        return jsonify(
            error="НЕ ТА ЯЧЕЙКА: требуется " + str(task[5]) + ", отсканирована " + str(location[0]),
            expected_location=task[5], actual_location=location[0],
        ), 409
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql("""
                UPDATE replenishment_tasks
                SET verified_location_barcode = ?, verified_location_at = ?
                WHERE id = ? AND status = 'started' AND assigned_employee_id = ?
                  AND verify_required = 1 AND verified_product_at IS NOT NULL
                  AND verified_location_at IS NULL
            """), (code, now_iso(), task_id, emp["id"]))
            if cur.rowcount != 1:
                return jsonify(error="Задача изменилась. Обнови экран."), 409
            _audit(cur, task_id, emp, "location_verified", code)
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True, stage="location", location=location[0])
