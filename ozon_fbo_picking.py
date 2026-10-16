"""Ozon FBO phase 2: server-side picking, independent from WB and MoySklad.

All mutations are serialized per cargo and idempotent by the client's request_id.
Confirmed quantities are stored in PostgreSQL/SQLite, not browser storage.
"""
import re
import sqlite3
import threading
import uuid

from flask import Blueprint, jsonify, redirect, request
from wms import DATABASE_URL, db_connect, ensure_db, get_session_employee, manager_access, now_iso, query_all, query_one, sql
from ozon_fbo import _db as ensure_manifest_db
from ozon_fbo_picking_ui import WORKER_PICK_HTML, MANAGER_PICK_HTML

fbo_picking_bp = Blueprint("fbo_picking", __name__)
_picking_lock = threading.Lock()
_picking_ready = False


def _ensure_picking_db():
    global _picking_ready
    ensure_db()
    ensure_manifest_db()
    if _picking_ready:
        return
    with _picking_lock:
        if _picking_ready:
            return
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                additions = {
                    "ozon_fbo_cargo": (
                        ("claimed_employee_id", "TEXT"),
                        ("claimed_at", "TEXT"),
                        ("completed_at", "TEXT"),
                    ),
                    "ozon_fbo_items": (("picked_qty", "INTEGER NOT NULL DEFAULT 0"),),
                }
                for table, extra in additions.items():
                    if DATABASE_URL:
                        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
                        existing = {row[0] for row in cur.fetchall()}
                    else:
                        cur.execute("PRAGMA table_info(" + table + ")")
                        existing = {row[1] for row in cur.fetchall()}
                    for name, ddl in extra:
                        if name not in existing:
                            cur.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + ddl)
                cur.execute("""CREATE TABLE IF NOT EXISTS ozon_fbo_pick_events (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL UNIQUE,
                    cargo_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    barcode TEXT NOT NULL,
                    quantity INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    voided_at TEXT,
                    FOREIGN KEY(cargo_id) REFERENCES ozon_fbo_cargo(id),
                    FOREIGN KEY(item_id) REFERENCES ozon_fbo_items(id)
                )""")
                cur.execute("""CREATE TABLE IF NOT EXISTS ozon_fbo_pick_audit (
                    id TEXT PRIMARY KEY,
                    cargo_id TEXT NOT NULL,
                    employee_id TEXT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    request_id TEXT
                )""")
                # Backwards-compatible migration in case a previous deployment created the audit table.
                if DATABASE_URL:
                    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s",
                                ("ozon_fbo_pick_audit",))
                    audit_cols = {r[0] for r in cur.fetchall()}
                else:
                    cur.execute("PRAGMA table_info(ozon_fbo_pick_audit)")
                    audit_cols = {r[1] for r in cur.fetchall()}
                if "request_id" not in audit_cols:
                    cur.execute("ALTER TABLE ozon_fbo_pick_audit ADD COLUMN request_id TEXT")
                cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fbo_audit_request
                    ON ozon_fbo_pick_audit(request_id) WHERE request_id IS NOT NULL""")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_fbo_pick_events_cargo ON ozon_fbo_pick_events(cargo_id,created_at)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_fbo_pick_audit_cargo ON ozon_fbo_pick_audit(cargo_id,created_at)")
                # One ongoing cargo per employee, across all Ozon FBO supplies.
                cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_fbo_one_cargo_per_employee
                    ON ozon_fbo_cargo(claimed_employee_id)
                    WHERE status = 'started' AND claimed_employee_id IS NOT NULL""")
                conn.commit()
                _picking_ready = True
            finally:
                cur.close()


def _scan_code(value):
    value = str(value or "").strip().replace("\r", "").replace("\n", "").replace("\t", "")
    if len(value) >= 3 and value[0] == "]" and value[1].isalpha() and value[2].isdigit():
        value = value[3:]
    return value


def _worker():
    employee = get_session_employee()
    return employee


def _locked(cur, cargo_id):
    query = """SELECT id,supply_id,barcode,status,claimed_employee_id,planned_qty
               FROM ozon_fbo_cargo WHERE id = ?"""
    if DATABASE_URL:
        query += " FOR UPDATE"
    cur.execute(sql(query), (cargo_id,))
    return cur.fetchone()


def _begin(cur):
    if not DATABASE_URL:
        cur.execute("BEGIN IMMEDIATE")


def _serialize(cargo_id):
    row = query_one("""
        SELECT c.id,c.supply_id,c.barcode,c.destination,c.label,c.planned_qty,
               c.status,c.claimed_employee_id,c.claimed_at,c.completed_at,
               s.title,e.name
        FROM ozon_fbo_cargo c
        JOIN ozon_fbo_supplies s ON s.id = c.supply_id
        LEFT JOIN employees e ON e.id = c.claimed_employee_id
        WHERE c.id = ?
    """, (cargo_id,))
    if not row:
        return None
    items = query_all("""
        SELECT id, article,barcode,planned_qty,picked_qty
        FROM ozon_fbo_items WHERE cargo_id = ? ORDER BY article,barcode
    """, (cargo_id,))
    prepared = [{"id": x[0], "article": x[1], "barcode": x[2],
                 "qty": x[3], "picked": x[4], "remaining": x[3] - x[4]} for x in items]
    return {
        "id": row[0], "supply_id": row[1], "barcode": row[2],
        "destination": row[3], "label": row[4], "qty": row[5],
        "status": row[6], "employee_id": row[7],
        "claimed_at": row[8], "completed_at": row[9],
        "supply_title": row[10], "employee": row[11],
        "picked": sum(it["picked"] for it in prepared),
        "remaining": sum(it["remaining"] for it in prepared),
        "items": prepared,
    }


def _audit(cur, cargo_id, employee_id, actor, action, reason="", request_id=None):
    cur.execute(sql("""INSERT INTO ozon_fbo_pick_audit
        (id,cargo_id,employee_id,actor,action,reason,created_at,request_id)
        VALUES (?,?,?,?,?,?,?,?)"""),
        (str(uuid.uuid4()), cargo_id, employee_id, actor, action, reason, now_iso(), request_id))


@fbo_picking_bp.get("/ozon-fbo/pick")
def picking_screen():
    if not _worker():
        return redirect("/login")
    _ensure_picking_db()
    return WORKER_PICK_HTML


@fbo_picking_bp.get("/manager/ozon-fbo/picking")
def picking_manager_screen():
    if not manager_access():
        return redirect("/manager-login")
    _ensure_picking_db()
    return MANAGER_PICK_HTML


@fbo_picking_bp.get("/api/ozon-fbo/pick/active")
def active_cargo():
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    row = query_one("""SELECT id FROM ozon_fbo_cargo
        WHERE status='started' AND claimed_employee_id=? ORDER BY claimed_at DESC LIMIT 1""",
        (employee["id"],))
    return jsonify(active=bool(row), cargo_id=row[0] if row else None)


@fbo_picking_bp.get("/api/ozon-fbo/pick/cargo/<cargo_id>")
def cargo_details(cargo_id):
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    item = _serialize(cargo_id)
    if not item:
        return jsonify(error="Грузоместо не найдено"), 404
    return jsonify(cargo=item, own=bool(item["employee_id"] == employee["id"]))


@fbo_picking_bp.post("/api/ozon-fbo/pick/cargo/<cargo_id>/claim")
def claim_cargo(cargo_id):
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            _begin(cur)
            cargo = _locked(cur, cargo_id)
            if not cargo:
                return jsonify(error="Грузоместо не найдено"), 404
            if cargo[3] == "done":
                return jsonify(error="Грузоместо уже собрано"), 409
            if cargo[4] and cargo[4] != employee["id"]:
                return jsonify(error="Это грузоместо закреплено за другим сотрудником"), 409
            if cargo[4] != employee["id"]:
                cur.execute(sql("""SELECT id FROM ozon_fbo_cargo
                    WHERE status='started' AND claimed_employee_id=? AND id<>? LIMIT 1"""),
                    (employee["id"], cargo_id))
                busy = cur.fetchone()
                if busy:
                    return jsonify(error="У тебя уже открыто другое грузоместо. Сначала заверши его.", active_cargo_id=busy[0]), 409
                cur.execute(sql("""UPDATE ozon_fbo_cargo
                    SET claimed_employee_id=?, claimed_at=?, status='started'
                    WHERE id=?"""), (employee["id"], now_iso(), cargo_id))
                _audit(cur, cargo_id, employee["id"], employee["name"], "claim")
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось закрепить грузоместо; возможно, оно уже занято"), 409
        finally:
            cur.close()
    return jsonify(ok=True, cargo=_serialize(cargo_id))


@fbo_picking_bp.post("/api/ozon-fbo/pick/cargo/<cargo_id>/scan")
def record_pick(cargo_id):
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    data = request.get_json(silent=True) or {}
    barcode = _scan_code(data.get("barcode"))
    qty = data.get("quantity")
    request_id = str(data.get("request_id") or "").strip()
    if not barcode or len(barcode) > 100:
        return jsonify(error="Сканируй штрихкод товара"), 400
    if type(qty) is not int or not 1 <= qty <= 100000:
        return jsonify(error="Укажи целое положительное количество"), 400
    try:
        uuid.UUID(request_id)
    except (ValueError, AttributeError):
        return jsonify(error="Некорректный номер операции. Обнови страницу."), 400

    with db_connect() as conn:
        cur = conn.cursor()
        try:
            _begin(cur)
            cargo = _locked(cur, cargo_id)
            if not cargo:
                return jsonify(error="Грузоместо не найдено"), 404
            cur.execute(sql("""SELECT cargo_id,employee_id,barcode,quantity,voided_at FROM ozon_fbo_pick_events
                WHERE request_id=?"""), (request_id,))
            duplicate = cur.fetchone()
            if duplicate:
                if duplicate[:4] != (cargo_id, employee["id"], barcode, qty):
                    return jsonify(error="Номер операции уже использован для другого скана"), 409
                if duplicate[4]:
                    return jsonify(error="Этот скан уже был отменён"), 409
                conn.commit()
                return jsonify(ok=True, replayed=True, cargo=_serialize(cargo_id))
            if cargo[3] != "started" or cargo[4] != employee["id"]:
                return jsonify(error="Сначала закрепи грузоместо за своей учётной записью"), 409
            cur.execute(sql("""SELECT id,article,planned_qty,picked_qty FROM ozon_fbo_items
                WHERE cargo_id=? AND barcode=?"""), (cargo_id, barcode))
            row = cur.fetchone()
            if not row:
                return jsonify(error="Неверный товар: такого штрихкода нет в этом грузоместе"), 422
            remaining = row[2] - row[3]
            if qty > remaining:
                return jsonify(error=f"Пересборка: нужно ещё {remaining} шт., введено {qty}"), 409
            cur.execute(sql("""UPDATE ozon_fbo_items SET picked_qty=picked_qty+?
                WHERE id=? AND picked_qty+?<=planned_qty"""), (qty, row[0], qty))
            if cur.rowcount != 1:
                conn.rollback()
                return jsonify(error="Количество изменилось. Обнови данные и повтори скан"), 409
            cur.execute(sql("""INSERT INTO ozon_fbo_pick_events
                (id,request_id,cargo_id,item_id,employee_id,barcode,quantity,created_at)
                VALUES (?,?,?,?,?,?,?,?)"""),
                (str(uuid.uuid4()), request_id, cargo_id, row[0], employee["id"], barcode, qty, now_iso()))
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось сохранить скан. Повтори с тем же номером операции."), 503
        finally:
            cur.close()
    return jsonify(ok=True, replayed=False, cargo=_serialize(cargo_id))


@fbo_picking_bp.post("/api/ozon-fbo/pick/cargo/<cargo_id>/undo")
def undo_pick(cargo_id):
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    request_id = str((request.get_json(silent=True) or {}).get("request_id") or "").strip()
    try:
        uuid.UUID(request_id)
    except (ValueError, AttributeError):
        return jsonify(error="Некорректный номер отмены"), 400
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            _begin(cur)
            cargo = _locked(cur, cargo_id)
            if not cargo:
                return jsonify(error="Грузоместо не найдено"), 404
            cur.execute(sql("""SELECT cargo_id,employee_id,action FROM ozon_fbo_pick_audit
                WHERE request_id=?"""), (request_id,))
            previous = cur.fetchone()
            if previous:
                if previous != (cargo_id, employee["id"], "undo"):
                    return jsonify(error="Номер отмены уже использован"), 409
                conn.commit()
                return jsonify(ok=True, replayed=True, cargo=_serialize(cargo_id))
            if cargo[3] != "started" or cargo[4] != employee["id"]:
                return jsonify(error="Только исполнитель может исправить незавершённое грузоместо"), 409
            cur.execute(sql("""SELECT id,item_id,quantity FROM ozon_fbo_pick_events
                WHERE cargo_id=? AND voided_at IS NULL ORDER BY created_at DESC,id DESC LIMIT 1"""), (cargo_id,))
            last = cur.fetchone()
            if not last:
                return jsonify(error="В этом грузоместе пока нечего отменять"), 409
            cur.execute(sql("UPDATE ozon_fbo_pick_events SET voided_at=? WHERE id=? AND voided_at IS NULL"),
                        (now_iso(), last[0]))
            if cur.rowcount != 1:
                conn.rollback()
                return jsonify(error="Этот скан уже отменён"), 409
            cur.execute(sql("""UPDATE ozon_fbo_items SET picked_qty=picked_qty-?
                WHERE id=? AND picked_qty>=?"""), (last[2], last[1], last[2]))
            if cur.rowcount != 1:
                conn.rollback()
                return jsonify(error="Ошибка количества. Изменения не применены."), 409
            _audit(cur, cargo_id, employee["id"], employee["name"], "undo",
                   f"Отмена последнего подтверждения: {last[2]} шт.", request_id=request_id)
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Ошибка отмены. Обнови данные."), 503
        finally:
            cur.close()
    return jsonify(ok=True, cargo=_serialize(cargo_id))


@fbo_picking_bp.post("/api/ozon-fbo/pick/cargo/<cargo_id>/complete")
def finish_cargo(cargo_id):
    employee = _worker()
    if not employee:
        return jsonify(error="Войди по бейджу"), 401
    _ensure_picking_db()
    barcode = _scan_code((request.get_json(silent=True) or {}).get("cargo_barcode"))
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            _begin(cur)
            cargo = _locked(cur, cargo_id)
            if not cargo:
                return jsonify(error="Грузоместо не найдено"), 404
            if cargo[3] == "done":
                if cargo[4] == employee["id"] and barcode == cargo[2]:
                    conn.commit()
                    return jsonify(ok=True, already_done=True, cargo=_serialize(cargo_id))
                return jsonify(error="Грузоместо уже закрыто"), 409
            if cargo[3] != "started" or cargo[4] != employee["id"]:
                return jsonify(error="Грузоместо закреплено за другим сотрудником"), 403
            if barcode != cargo[2]:
                return jsonify(error="Неверный ШК грузоместа. Сканируй этикетку именно этого грузоместа."), 422
            cur.execute(sql("""SELECT COUNT(*), COALESCE(SUM(planned_qty - picked_qty),0)
                FROM ozon_fbo_items WHERE cargo_id=?"""), (cargo_id,))
            count,remaining = cur.fetchone()
            if not count or remaining:
                return jsonify(error=f"Сначала дособери все позиции. Осталось {remaining} шт."), 409
            cur.execute(sql("UPDATE ozon_fbo_cargo SET status='done',completed_at=? WHERE id=?"),
                        (now_iso(), cargo_id))
            _audit(cur, cargo_id, employee["id"], employee["name"], "complete",
                   "ШК грузоместа подтверждён")
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Не удалось закрыть грузоместо"), 503
        finally:
            cur.close()
    return jsonify(ok=True, cargo=_serialize(cargo_id))


@fbo_picking_bp.get("/api/manager/ozon-fbo/progress")
def manager_progress():
    if not manager_access():
        return jsonify(error="Требуется доступ руководителя"), 403
    _ensure_picking_db()
    rows = query_all("""SELECT c.id,c.barcode,c.destination,c.label,c.planned_qty,
        c.status,c.claimed_employee_id,e.name,c.completed_at,s.title,s.id,
        COALESCE(SUM(i.picked_qty),0)
        FROM ozon_fbo_cargo c JOIN ozon_fbo_supplies s ON c.supply_id=s.id
        LEFT JOIN ozon_fbo_items i ON c.id=i.cargo_id
        LEFT JOIN employees e ON c.claimed_employee_id=e.id
        GROUP BY c.id,c.barcode,c.destination,c.label,c.planned_qty,c.status,
                 c.claimed_employee_id,e.name,c.completed_at,s.title,s.id
        ORDER BY s.created_at DESC,c.destination,c.label LIMIT 1000""")
    result = [{"id":r[0],"barcode":r[1],"destination":r[2],"label":r[3],
               "qty":r[4],"status":r[5],"employee_id":r[6],"employee":r[7],
               "completed_at":r[8],"supply":r[9],"supply_id":r[10],"picked":r[11]}
              for r in rows]
    return jsonify(cargo=result)


@fbo_picking_bp.post("/api/manager/ozon-fbo/release")
def manager_release():
    if not manager_access():
        return jsonify(error="Требуется доступ руководителя"), 403
    _ensure_picking_db()
    data = request.get_json(silent=True) or {}
    cargo_id = str(data.get("cargo_id") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if not cargo_id or not 8 <= len(reason) <= 500:
        return jsonify(error="Укажи грузоместо и причину передачи (минимум 8 символов)"), 400
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            _begin(cur)
            cargo = _locked(cur, cargo_id)
            if not cargo:
                return jsonify(error="Грузоместо не найдено"), 404
            if cargo[3] == "done":
                return jsonify(error="Завершённое грузоместо нельзя передать"), 409
            if not cargo[4]:
                return jsonify(error="Грузоместо свободно"), 409
            cur.execute(sql("""UPDATE ozon_fbo_cargo
                SET claimed_employee_id=NULL,claimed_at=NULL WHERE id=?"""),(cargo_id,))
            _audit(cur,cargo_id,cargo[4],"Руководитель (ПК)","release",reason)
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Ошибка передачи грузоместа"), 503
        finally:
            cur.close()
    return jsonify(ok=True)


@fbo_picking_bp.get("/api/manager/ozon-fbo/audit/<cargo_id>")
def manager_audit(cargo_id):
    if not manager_access():
        return jsonify(error="Требуется доступ руководителя"), 403
    _ensure_picking_db()
    rows = query_all("""SELECT a.actor,a.action,a.reason,a.created_at
        FROM ozon_fbo_pick_audit a WHERE a.cargo_id=? ORDER BY a.created_at DESC LIMIT 100""",(cargo_id,))
    picks = query_all("""SELECT e.name,p.barcode,p.quantity,p.created_at,p.voided_at
        FROM ozon_fbo_pick_events p LEFT JOIN employees e ON e.id=p.employee_id
        WHERE p.cargo_id=? ORDER BY p.created_at DESC LIMIT 250""",(cargo_id,))
    return jsonify(actions=[{"actor":r[0],"action":r[1],"reason":r[2],"at":r[3]} for r in rows],
                   scans=[{"employee":r[0],"barcode":r[1],"qty":r[2],"at":r[3],
                           "voided":bool(r[4])} for r in picks])
