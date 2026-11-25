"""Manager-only printer registry for the isolated GRIT line-routing pilot.

Feature is OFF by default. No WB API reads, order mutations, job reservations,
background workers or physical printing. QR identity is not employee login.
"""
import os
import secrets
import uuid
from html import escape

from flask import Blueprint, jsonify, redirect, request, session, Response

from wms import db_connect, manager_access, now_iso, sql
from line_printer_registry import create_printer, normalize_lines, printable_58x40_html, Printer
from line_printer_routing import route_order_to_printer

line_printers_bp = Blueprint("line_printers", __name__)


def _enabled():
    return os.environ.get("GRIT_LINE_PRINTER_ROUTING_ENABLED", "0") == "1"


def _gate():
    if not _enabled():
        return jsonify(error="Модуль маршрутизации принтеров отключён"), 404
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    return None


def _initialize():
    # This is called only after feature flag and manager auth checks.
    with db_connect() as db:
        c = db.cursor()
        c.execute(sql("""
            CREATE TABLE IF NOT EXISTS grit_line_printers (
              printer_id TEXT PRIMARY KEY,
              name TEXT NOT NULL,
              lines_json TEXT NOT NULL,
              token_digest TEXT NOT NULL,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
        """))
        db.commit()
        c.close()


def _load():
    import json
    _initialize()
    with db_connect() as db:
        c = db.cursor()
        c.execute("SELECT printer_id,name,lines_json,token_digest,enabled FROM grit_line_printers ORDER BY name,printer_id")
        records = c.fetchall()
        c.close()
    return [Printer(str(r[0]), str(r[1]), tuple(json.loads(r[2])),
                    str(r[3]), bool(r[4])) for r in records]


def _token():
    if "grit_printer_csrf" not in session:
        session["grit_printer_csrf"] = secrets.token_urlsafe(32)
    return session["grit_printer_csrf"]


def _csrf_ok(body):
    return isinstance(body, dict) and secrets.compare_digest(
        str(body.get("csrf") or ""), _token())


def _record(p):
    return {"printer_id": p.printer_id, "name": p.name,
            "lines": list(p.lines), "enabled": p.enabled}


@line_printers_bp.get("/api/manager/line-printers")
def list_printers():
    denial = _gate()
    if denial is not None:
        return denial
    return jsonify(printers=[_record(p) for p in _load()], csrf=_token(),
                   feature="synthetic-pilot", printing_enabled=False)


@line_printers_bp.post("/api/manager/line-printers")
def add_printer():
    denial = _gate()
    if denial is not None:
        return denial
    body = request.get_json(silent=True)
    if not _csrf_ok(body):
        return jsonify(error="Неверный CSRF"), 403
    try:
        p, payload = create_printer(str(uuid.uuid4()), body.get("name"),
                                    body.get("lines"))
    except (ValueError, TypeError):
        return jsonify(error="Проверь название принтера и список линий"), 400
    import json
    _initialize()
    with db_connect() as db:
        c = db.cursor()
        c.execute(sql("INSERT INTO grit_line_printers "
                      "(printer_id,name,lines_json,token_digest,enabled,created_at,updated_at)"
                      " VALUES (?,?,?,?,?,?,?)"),
                  (p.printer_id, p.name, json.dumps(p.lines, ensure_ascii=False),
                   p.token_digest, 1, now_iso(), now_iso()))
        db.commit()
        c.close()
    # One-time QR label preview: never persisted server-side.
    return jsonify(printer=_record(p), label_html=printable_58x40_html(p, payload),
                   warning="QR для принтера создан. Сохрани этикетку: повторный выпуск отзовёт прежний QR."), 201


@line_printers_bp.post("/api/manager/line-printers/<printer_id>/update")
def update_printer(printer_id):
    denial = _gate()
    if denial is not None:
        return denial
    body = request.get_json(silent=True)
    if not _csrf_ok(body):
        return jsonify(error="Неверный CSRF"), 403
    current = next((p for p in _load() if p.printer_id == printer_id), None)
    if current is None:
        return jsonify(error="Принтер не найден"), 404
    name = body.get("name", current.name)
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        return jsonify(error="Некорректное название"), 400
    try:
        lines = normalize_lines(body.get("lines", list(current.lines)))
    except (ValueError, TypeError):
        return jsonify(error="Некорректные линии"), 400
    enabled = body.get("enabled", current.enabled)
    if not isinstance(enabled, bool):
        return jsonify(error="Некорректный статус"), 400
    import json
    with db_connect() as db:
        c = db.cursor()
        c.execute(sql("""UPDATE grit_line_printers
            SET name=?, lines_json=?, enabled=?, updated_at=?
            WHERE printer_id=?"""), (name.strip(),
            json.dumps(lines, ensure_ascii=False), int(enabled), now_iso(), printer_id))
        db.commit()
        c.close()
    return jsonify(printer={"printer_id": printer_id, "name": name.strip(),
                             "lines": list(lines), "enabled": enabled})


@line_printers_bp.post("/api/manager/line-printers/<printer_id>/rotate")
def rotate_qr(printer_id):
    denial = _gate()
    if denial is not None:
        return denial
    body = request.get_json(silent=True)
    if not _csrf_ok(body):
        return jsonify(error="Неверный CSRF"), 403
    current = next((p for p in _load() if p.printer_id == printer_id), None)
    if current is None:
        return jsonify(error="Принтер не найден"), 404
    if not current.enabled:
        return jsonify(error="Отключённый принтер нельзя активировать QR"), 409
    from line_printer_registry import rotate_printer_token
    updated, payload = rotate_printer_token(current)
    with db_connect() as db:
        c = db.cursor()
        c.execute(sql("UPDATE grit_line_printers SET token_digest=?,updated_at=? WHERE printer_id=?"),
                  (updated.token_digest, now_iso(), printer_id))
        db.commit()
        c.close()
    return jsonify(printer=_record(updated),
                   label_html=printable_58x40_html(updated, payload),
                   warning="Старый QR отозван.")


PAGE = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Принтеры по линиям</title>
<style>
*{box-sizing:border-box}body{background:#0c1423;color:#edf3fc;margin:0;font:15px system-ui,Arial}
main{max-width:1100px;margin:auto;padding:20px}a{color:#8abcfb}
h1{font-size:25px}.sub{color:#a5b5ca}.panel{background:#172235;border:1px solid #33445b;border-radius:14px;padding:18px;margin:14px 0}
input{background:#101b2c;border:1px solid #42546c;color:#fff;border-radius:8px;padding:10px;font:inherit;width:100%;max-width:450px}
label{display:block;margin:12px 0 5px;color:#bcd0e8}
button{background:#3868bf;color:white;border:0;border-radius:9px;padding:11px 15px;cursor:pointer;font-weight:700;margin:6px 6px 6px 0}
button:disabled{opacity:.5}.secondary{background:#31435d}
.item{border-top:1px solid #33445b;padding:14px 0}.item:first-child{border-top:0}
small{color:#a5b5ca}#notice{white-space:pre-wrap;margin:10px 0;color:#facd87}
</style></head><body><main>
<a href="/manager">← Панель руководителя</a><h1>Принтеры по линиям</h1>
<p class="sub">Пилот: создание принтера и выпуск QR 58×40. Реальная печать заказов WB и выдача заданий отключены.</p>
<section class="panel"><h2>Новый принтер</h2>
<label for="name">Название</label><input id="name" maxlength="80" placeholder="Например, Принтер А-Б">
<label for="lines">Линии через запятую</label><input id="lines" placeholder="А, Б">
<div><button id="create">Создать и получить QR 58×40</button></div>
</section><section class="panel"><h2>Зарегистрированные принтеры</h2><div id="printers"></div></section>
<div id="notice" role="alert"></div>
<script>
let csrf='';
const byId=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
const lines=s=>s.split(',').map(x=>x.trim()).filter(Boolean);
async function api(url,options){
 const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...options});
 const j=await r.json();if(!r.ok)throw Error(j.error||'Ошибка API');return j;
}
function labelWindow(html){
 const blob=new Blob([html],{type:'text/html;charset=utf-8'});
 const url=URL.createObjectURL(blob);
 const a=document.createElement('a');a.href=url;a.download='grit-printer-58x40.html';
 a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
 byId('notice').textContent='Этикетка сохранена как HTML. Открой файл и печатай вручную на 58×40 мм. Физическая печать не подтверждается сервером.';
}
async function refresh(){
 const j=await api('/api/manager/line-printers');csrf=j.csrf;
 byId('printers').innerHTML=j.printers.map(p=>`<div class="item"><b>${esc(p.name)}</b>
 <p><small>ID ${esc(p.printer_id)} · Линии: ${esc(p.lines.join(', '))}</small></p>
 <p><small>${p.enabled?'Активен':'Отключён'}</small></p>
 <button class="secondary" data-action="toggle" data-id="${esc(p.printer_id)}" data-enabled="${p.enabled}">${p.enabled?'Отключить':'Включить'}</button>
 <button class="secondary" data-action="rotate" data-id="${esc(p.printer_id)}" ${!p.enabled?'disabled':''}>Перевыпустить QR</button></div>`).join('')||'<small>Принтеров пока нет.</small>';
}
byId('create').onclick=async()=>{
 try{const j=await api('/api/manager/line-printers',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({csrf,name:byId('name').value,lines:lines(byId('lines').value)})});
 labelWindow(j.label_html);await refresh();
 }catch(e){byId('notice').textContent=e.message;}
};
byId('printers').onclick=async ev=>{
 const b=ev.target.closest('button[data-action]');if(!b)return;
 const id=b.dataset.id,action=b.dataset.action;
 if(action==='rotate'&&!confirm('Старый QR перестанет работать. Выпустить новый?'))return;
 try{
 const url='/api/manager/line-printers/'+encodeURIComponent(id)+'/'+(action==='rotate'?'rotate':'update');
 const body=action==='rotate'?{csrf}:{csrf,enabled:b.dataset.enabled!=='true'};
 const j=await api(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 if(j.label_html)labelWindow(j.label_html);await refresh();
 }catch(e){byId('notice').textContent=e.message;}
};
refresh().catch(e=>byId('notice').textContent=e.message);
</script></main></body></html>"""


@line_printers_bp.get("/manager/line-printers")
def page():
    denial = _gate()
    if denial is not None:
        return redirect("/manager-login") if manager_access() is False and _enabled() else denial
    return Response(PAGE, mimetype="text/html", headers={"Cache-Control": "no-store"})
