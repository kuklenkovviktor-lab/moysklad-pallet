"""Minimal opt-in cloud-to-local ONIX printing bridge (TEST LABELS ONLY).

No WB labels, order identifiers, or arbitrary printer commands are transmitted.
The Windows agent always generates its own fixed non-WB diagnostic label.
No automatic retry after claiming a job, since print outcome may be uncertain.
"""
import hmac
import os
import secrets
import uuid
from datetime import datetime, timezone
from html import escape

from flask import Blueprint, Response, abort, jsonify, redirect, request, session

from wms import db_connect, manager_access, sql

print_bridge_bp = Blueprint("onix_print_bridge", __name__)
TEST_KIND = "onix-test-only"
MAX_LIST = 10


def _now():
    return datetime.now(timezone.utc).isoformat()


def _shared_secret():
    token = os.environ.get("ONIX_PRINT_AGENT_TOKEN", "")
    return token if len(token) >= 32 else ""


def _enabled():
    return bool(_shared_secret())


def _agent_authorized():
    token = _shared_secret()
    if not token:
        return False
    supplied = request.headers.get("Authorization", "")
    return hmac.compare_digest(supplied, "Bearer " + token)


def _agent_gate():
    if not _agent_authorized():
        # Do not distinguish disabled from wrong credentials.
        abort(401)


def _init_db():
    with db_connect() as db:
        db.cursor().execute("""CREATE TABLE IF NOT EXISTS onix_test_print_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            claimed_at TEXT,
            completed_at TEXT
        )""")
        db.cursor().execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_onix_test_one_unresolved
            ON onix_test_print_jobs(kind)
            WHERE status IN ('queued','claimed','uncertain')""")
        db.commit()


def _csrf():
    if not session.get("onix_print_csrf"):
        session["onix_print_csrf"] = secrets.token_urlsafe(24)
    return session["onix_print_csrf"]


@print_bridge_bp.get("/manager/onix-print-test")
def test_print_page():
    if not manager_access():
        return redirect("/manager-login")
    _init_db()
    with db_connect() as db:
        rows=db.cursor().execute(
            sql("SELECT id,status,created_at FROM onix_test_print_jobs "
                "ORDER BY created_at DESC,id DESC LIMIT ?"), (MAX_LIST,)
        ).fetchall()
    safe_csrf=escape(_csrf(),quote=True)
    state="Мост готов" if _enabled() else (
        "Мост отключён: ONIX_PRINT_AGENT_TOKEN не задан в Render или короче 32 символов."
    )
    disable="" if _enabled() else " disabled"
    items="".join(
        "<tr><td>"+escape(r[0][:12])+"</td><td>"+escape(r[1])+
        "</td><td>"+escape(r[2][:19])+"</td></tr>" for r in rows
    )
    return Response("""<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Тест USB-печати</title>
<style>body{font:16px system-ui,sans-serif;background:#e9eef3;color:#223443;margin:30px}
main{max-width:780px;margin:auto;background:#fff;border-radius:14px;padding:24px}
button{padding:14px 20px;border:0;border-radius:8px;background:#365f79;color:white;font-size:16px}
button:disabled{opacity:.4}table{width:100%;text-align:left;border-collapse:collapse}
td,th{padding:10px;border-bottom:1px solid #dde7ed}</style></head><body><main>
<a href="/manager">← Менеджерская панель</a><h1>Тестовая печать ONIX</h1>
<p>"""+escape(state)+"""</p><p><strong>Только одна пробная этикетка ONIX TEST / NOT WB.</strong>
Рабочие ШК Wildberries и остатки здесь не используются.</p>
<form action="/manager/onix-print-test" method="post">
<input type="hidden" name="csrf" value='"""+safe_csrf+"""'>
<input type="hidden" name="confirmation" value="one-test-label">
<button type="submit" """+disable+""">Создать одно тестовое задание</button></form>
<p>После создания задания агент на складском Windows-ПК должен быть запущен
вручную командой --once. Повторной автоматической печати нет.</p>
<h2>Последние задания</h2>
<table><thead><tr><th>ID</th><th>Статус</th><th>Создано UTC</th></tr></thead>
<tbody>"""+items+"""</tbody></table></main></body></html>""",
        mimetype="text/html",headers={"Cache-Control":"no-store"})


@print_bridge_bp.post("/manager/onix-print-test")
def test_print_enqueue():
    if not manager_access():
        abort(403)
    token=str(request.form.get("csrf",""))
    if not hmac.compare_digest(token,_csrf()):
        abort(403)
    if request.form.get("confirmation")!="one-test-label":
        abort(400)
    if not _enabled():
        return jsonify(error="Мост печати отключён"),503
    _init_db()
    with db_connect() as db:
        cur=db.cursor()
        # Only one unresolved item at a time; claimed jobs are never requeued.
        waiting=cur.execute(sql(
            "SELECT id FROM onix_test_print_jobs "
            "WHERE status IN ('queued','claimed','uncertain') LIMIT 1"
        )).fetchone()
        if waiting:
            return jsonify(error="Уже существует незавершённое или сомнительное задание"),409
        cur.execute(sql(
            "INSERT INTO onix_test_print_jobs(id,kind,status,created_at) VALUES (?,?,?,?) "
            "ON CONFLICT DO NOTHING"
        ),(str(uuid.uuid4()),TEST_KIND,"queued",_now()))
        created=cur.rowcount
        db.commit()
    if created!=1:
        return jsonify(error="Задание уже ожидает выполнения"),409
    return redirect("/manager/onix-print-test",code=303)


@print_bridge_bp.get("/api/print-agent/test/next")
def agent_next():
    _agent_gate()
    _init_db()
    with db_connect() as db:
        cur=db.cursor()
        row=cur.execute(sql(
            "UPDATE onix_test_print_jobs SET status='claimed',claimed_at=? "
            "WHERE id=(SELECT id FROM onix_test_print_jobs WHERE status='queued' "
            "ORDER BY created_at,id LIMIT 1) AND status='queued' RETURNING id"
        ),(_now(),)).fetchone()
        db.commit()
    if not row:
        return Response(status=204,headers={"Cache-Control":"no-store"})
    return jsonify(job_id=row[0],kind=TEST_KIND,printer="ONIX-TEST-XP460B")


@print_bridge_bp.post("/api/print-agent/test/ack")
def agent_ack():
    _agent_gate()
    data=request.get_json(silent=True) or {}
    job_id=str(data.get("job_id",""))
    status=str(data.get("status",""))
    if len(job_id)!=36 or status not in ("submitted","uncertain"):
        abort(400)
    _init_db()
    with db_connect() as db:
        cur=db.cursor()
        cur.execute(sql(
            "UPDATE onix_test_print_jobs SET status=?,completed_at=? "
            "WHERE id=? AND kind=? AND status='claimed'"
        ),(status,_now(),job_id,TEST_KIND))
        count=cur.rowcount
        db.commit()
    if count!=1:
        return jsonify(error="Задание не найдено или уже завершено"),409
    return jsonify(ok=True,status=status)
