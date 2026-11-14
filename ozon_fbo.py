"""Ozon FBO phase 1: plan-only XLSX import and cargo-place picking tasks.

Isolated from WB FBS, MoySklad stock balances and live scans.
"""
import hashlib
import json
import os
import threading
import uuid

from flask import Blueprint, jsonify, redirect, request
from ozon_fbo_parser import parse_ozon_xlsx
from ozon_fbo_catalog_validation import (CatalogUnavailable, check_catalog,
    resolved_manifest, resolved_plan_hash)
from wms import db_connect, ensure_db, get_session_employee, manager_access, now_iso, query_all, query_one, sql

ozon_fbo_bp = Blueprint("ozon_fbo", __name__)
_init_lock = threading.Lock()
_ready = False

MANAGER_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Ozon FBO</title><style>
*{box-sizing:border-box}body{font-family:system-ui,Arial;background:#e9eef3;color:#25313d;margin:0}
main{max-width:1160px;margin:auto;padding:18px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:15px;padding:17px;margin:13px 0}
h1{margin:15px 0 8px}h2{font-size:18px;margin:0 0 12px}a{color:#48647a}
.small{font-size:13px;color:#657683}.danger{color:#a43f3f}.warn{color:#955b1c}.ok{color:#27794f}
button{background:#48647a;border:0;border-radius:9px;padding:11px 16px;color:#fff;font-weight:750;cursor:pointer}
button:disabled{opacity:.45;cursor:wait}input{padding:11px;border:1px solid #cbd8e1;border-radius:9px;max-width:100%}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:12px 0}
.kpis{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.kpi{padding:12px;background:#eaf0f4;border-radius:10px}
.kpi b{font-size:24px;display:block;margin-top:3px}.kpi span{font-size:12px;color:#647684}
table{width:100%;border-collapse:collapse;background:#fff;min-width:600px}
th,td{border-bottom:1px solid #e3e9ed;padding:9px 10px;text-align:left;font-size:13px}
th{background:#eaf0f4}.scroll{overflow-x:auto}details{border-top:1px solid #dce4e9;padding:10px 0}
summary{cursor:pointer;font-weight:750}#warnings{margin-top:10px}
#importBtn{background:#2d6757}.tag{font-size:11px;background:#dce8ee;padding:3px 7px;border-radius:8px}
@media(max-width:650px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.controls{align-items:stretch;flex-direction:column}}
</style></head><body><main>
<a href="/manager">← Панель руководителя</a><h1>📦 Ozon FBO</h1>
<p><a href="/manager/ozon-fbo/picking">📊 Открыть монитор фактической сборки →</a></p>
<p class="small">Импорт Excel и создание плана грузомест. Фактическая сборка подтверждается на отдельном терминале сотрудника; остатки МойСклад не изменяются.</p>
<section class="card"><h2>Загрузить поставку</h2>
<div class="controls"><label>Название поставки <input id="title" maxlength="140" placeholder="Стекло · 17.10"></label>
<label>Excel-файл <input id="file" type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"></label>
<button id="previewBtn" type="button">Проверить файл</button></div>
<div id="notice" role="status" class="small"></div><div id="preview"></div>
<div id="warnings"></div>
<label id="warningConfirm" style="display:none;margin:14px 0"><input id="agree" type="checkbox"> Подтверждаю исключение точных повторов из плана</label>
<button id="importBtn" type="button" disabled>Создать задания по грузоместам</button></section>
<section class="card"><h2>Загруженные поставки</h2><div id="supplies">Загрузка…</div></section>
<section class="card"><h2>Состав поставки</h2><div id="detail" class="small">Выбери поставку в списке выше.</div></section>
<script>
const fileEl=document.getElementById('file'),notice=document.getElementById('notice');
const previewEl=document.getElementById('preview'),warningsEl=document.getElementById('warnings');
const importBtn=document.getElementById('importBtn');
let current=null,busy=false;
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function kpis(d){return '<div class="kpis">'+[['Направлений',(d.destinations||[]).length],['Грузомест',d.places],['Строк плана',d.unique_rows],['Товаров, шт.',d.qty]].map(x=>
 '<div class="kpi"><span>'+esc(x[0])+'</span><b>'+esc(x[1])+'</b></div>').join('')+'</div>'}
function itemsTable(items){return '<div class="scroll"><table><thead><tr><th>Артикул</th><th>ШК товара</th><th>Количество</th></tr></thead><tbody>'+
 items.map(i=>'<tr><td>'+esc(i.article)+'</td><td>'+esc(i.barcode)+'</td><td>'+esc(i.qty)+'</td></tr>').join('')+'</tbody></table></div>'}
function cargoBlock(p){return '<details><summary>'+esc(p.destination)+' · '+esc(p.label)+' · ГМ '+esc(p.barcode)+' · '+esc(p.qty)+' шт. · '+esc(p.items.length)+' поз.</summary>'+itemsTable(p.items)+'</details>'}
function resetPreview(){
 current=null;importBtn.disabled=true;previewEl.innerHTML='';warningsEl.innerHTML='';
 document.getElementById('agree').checked=false;
 document.getElementById('warningConfirm').style.display='none';
}
function checkButton(){importBtn.disabled=!current||current.errors.length>0||(current.warnings.length>0&&!document.getElementById('agree').checked)||busy}
document.getElementById('agree').addEventListener('change',checkButton);
fileEl.addEventListener('change',function(){
 resetPreview();
 const titleInput=document.getElementById('title');
 if(this.files[0] && !titleInput.value.trim()){
  titleInput.value=this.files[0].name.replace(/\.xlsx$/i,'').slice(0,140);
 }
});
async function sendUpload(url,extra){
 const file=fileEl.files[0];if(!file)throw Error('Выбери Excel .xlsx');
 const form=new FormData();form.append('file',file);
 Object.entries(extra||{}).forEach(([k,v])=>form.append(k,v));
 const r=await fetch(url,{method:'POST',body:form});
 const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка импорта');return d;
}
document.getElementById('previewBtn').addEventListener('click',async function(){
 if(busy)return;busy=true;resetPreview();this.disabled=true;notice.className='small';notice.textContent='Читаю Excel…';
 try{
  const d=await sendUpload('/api/manager/ozon-fbo/preview',{});
  current=d;previewEl.innerHTML=kpis(d)+'<div style="margin-top:12px">'+d.cargo.map(cargoBlock).join('')+'</div>';
  warningsEl.innerHTML=d.warnings.length?'<p class="warn"><b>Найдены точные повторы: '+d.warnings.length+'</b></p><ol>'+d.warnings.map(w=>'<li>'+esc(w)+'</li>').join('')+'</ol>':'<p class="ok">Точных повторов нет.</p>';
  if(d.errors.length){warningsEl.innerHTML+='<p class="danger"><b>Ошибки, импорт заблокирован:</b></p><ol>'+d.errors.map(x=>'<li>'+esc(x)+'</li>').join('')+'</ol>'}
  document.getElementById('warningConfirm').style.display=d.warnings.length?'block':'none';
  notice.textContent=d.errors.length?'Исправь Excel и проверь снова.':'Предпросмотр готов. Подтверди создание заданий.';
 }catch(e){notice.className='danger';notice.textContent=e.message}
 finally{busy=false;this.disabled=false;checkButton()}
});
importBtn.addEventListener('click',async function(){
 if(busy||!current||this.disabled)return;
 if(!document.getElementById('title').value.trim()){notice.className='danger';notice.textContent='Введи название поставки';return}
 if(!confirm('Создать '+current.places+' грузомест и план на '+current.qty+' шт. без подтверждения фактической сборки?'))return;
 busy=true;checkButton();notice.className='small';notice.textContent='Сохраняю поставку…';
 try{
  const d=await sendUpload('/api/manager/ozon-fbo/import',{
   title:document.getElementById('title').value.trim(),source_hash:current.source_hash,
   plan_hash:current.plan_hash,accept_duplicates:document.getElementById('agree').checked?'1':'0'
  });
  notice.className='ok';notice.textContent='Поставка создана. '+d.places+' грузомест, '+d.qty+' шт.';
  resetPreview();fileEl.value='';
  await loadSupplies();await loadDetail(d.id);
 }catch(e){notice.className='danger';notice.textContent=e.message}
 finally{busy=false;checkButton()}
});
async function loadSupplies(){
 const r=await fetch('/api/manager/ozon-fbo/supplies',{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Ошибка');
 document.getElementById('supplies').innerHTML=d.supplies.length?'<div class="scroll"><table><thead><tr><th>Поставка</th><th>Грузомест</th><th>План, шт.</th><th>Дата</th><th></th></tr></thead><tbody>'+
 d.supplies.map(s=>'<tr><td>'+esc(s.title)+'</td><td>'+esc(s.places)+'</td><td>'+esc(s.qty)+'</td><td>'+esc(new Date(s.created_at).toLocaleString('ru-RU'))+'</td><td><button type="button" data-id="'+esc(s.id)+'">Открыть</button></td></tr>').join('')+'</tbody></table></div>':'<p class="small">Поставок пока нет.</p>';
}
document.getElementById('supplies').addEventListener('click',e=>{const b=e.target.closest('button[data-id]');if(b)loadDetail(b.dataset.id).catch(x=>{document.getElementById('detail').textContent=x.message})});
async function loadDetail(id){
 const r=await fetch('/api/manager/ozon-fbo/supplies/'+encodeURIComponent(id),{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Ошибка');
 document.getElementById('detail').innerHTML='<h2>'+esc(d.supply.title)+'</h2><p class="small">Статус: план создан. ШК грузоместа сохранены без изменения цифр.</p>'+
 d.cargo.map(cargoBlock).join('');
}
loadSupplies().catch(e=>{document.getElementById('supplies').textContent=e.message});
</script></main></body></html>"""

WORKER_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e"><title>GRIT WMS · Ozon FBO · Задания</title><style>
*{box-sizing:border-box}body{font-family:system-ui,Arial;background:#e9eef3;color:#263643;margin:0}
main{max-width:840px;margin:auto;padding:15px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:15px;padding:15px;margin:12px 0}
h1{margin:14px 0}h2{font-size:19px}a{color:#48647a}.small{font-size:13px;color:#657683}
.item{border-top:1px solid #dce5eb;padding:11px 0}.item:first-child{border:0}.item summary{cursor:pointer;font-weight:750}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{padding:8px;text-align:left;border-bottom:1px solid #dfe7ec}

/* GRIT WMS unified dark worker screen. Manager page receives /grit-ui.css. */
:root{color-scheme:dark;--g-bg:#09121e;--g-panel:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca}
html,body{background:var(--g-bg);color:var(--g-text)}
.card{background:linear-gradient(145deg,#16283b,#101c2a);border-color:var(--g-line)}
h1,h2{color:#f1f6ff}a{color:#77c0ff}.small{color:var(--g-muted)}
.item{border-color:#30465c}th,td{color:#e4eef8;border-color:#2b4255}
a:focus-visible,summary:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
.item summary{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;min-height:44px}
.cargo-title{flex:1;min-width:140px}
.cargo-badge{display:inline-block;padding:5px 9px;border:1px solid;border-radius:8px;font-size:12px;font-weight:750}
.cargo-badge.free{background:#19344c;border-color:#39668b;color:#c4e9ff}
.cargo-badge.mine{background:#51351c;border-color:#a56a32;color:#ffd19d}
.cargo-badge.busy{background:#493229;border-color:#8b5541;color:#ffb49c}
.cargo-badge.done{background:#173b35;border-color:#2e705b;color:#81e9b9}
.cargo-progress{height:7px;background:#263c50;border-radius:9px;overflow:hidden;margin:10px 0}
.cargo-progress>div{height:100%;background:#ff9230}
.cargo-done .cargo-progress>div{background:#46c395}
.cargo-action{display:inline-block;padding:10px 12px;border-radius:9px;background:#176eae;color:#fff;font-weight:750;text-decoration:none}
.cargo-positions{border:1px solid #30465c;border-radius:10px;overflow:hidden}
.cargo-position{display:flex;align-items:center;gap:10px;padding:10px 9px;border-top:1px solid #30465c}
.cargo-position:first-child{border-top:0}
.position-pending{background:#142337}
.position-done{background:#173b35;color:#a4dfc9}
.position-icon{font-size:20px;color:#ff9230;min-width:18px}
.position-done .position-icon{color:#81e9b9}
.position-name{min-width:0;flex:1;overflow-wrap:anywhere}
.position-done .position-name b{color:#a4dfc9}
.position-count{font-variant-numeric:tabular-nums;font-weight:750;white-space:nowrap}
@media(max-width:620px){.item summary{align-items:flex-start}.cargo-badge{font-size:11px}.cargo-position{padding:12px 8px}}
</style></head><body><main><a href="/fbs">← Рабочая панель</a>
<h1>📦 Ozon FBO</h1><p class="small">Выбери грузоместо и начни сборку со сканером. Прогресс сохраняется на сервере.</p>
<p><a href="/ozon-fbo/pick">▶ Продолжить мою сборку</a></p>
<div id="list">Загрузка…</div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function cargoState(p){
 if(p.status==='done')return {kind:'done',label:'✓ Собрано',blocked:true};
 if(p.status==='started'&&p.own)return {kind:'mine',label:'● Моя сборка',blocked:false};
 if(p.status==='started')return {kind:'busy',label:'● Собирает '+(p.employee||'сотрудник'),blocked:true};
 return {kind:'free',label:'○ Свободно',blocked:false};
}
async function load(){
 const r=await fetch('/api/ozon-fbo/pick/overview',{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Не удалось загрузить задания');
 const list=document.getElementById('list');
 const expanded=new Set([...list.querySelectorAll('details[open][data-cargo]')].map(e=>e.dataset.cargo));
 list.innerHTML=d.supplies.length?d.supplies.map(s=>
  '<div class="card"><h2>'+esc(s.title)+'</h2><div class="small">'+esc(s.places)+' грузомест · '+esc(s.qty)+' шт. по плану</div>'+
  s.cargo.map(p=>{
   const state=cargoState(p),pct=p.qty?Math.min(100,100*p.picked/p.qty):0;
   const ordered=[...p.items].sort((a,b)=>Number(a.remaining===0)-Number(b.remaining===0));
   const action=state.blocked?'<p class="small">'+(state.kind==='busy'?'Занято другим сотрудником. Повторная сборка заблокирована.':'Грузоместо уже завершено.')+'</p>':
     '<p><a class="cargo-action" href="/ozon-fbo/pick?cargo_id='+encodeURIComponent(p.id)+'">'+(state.kind==='mine'?'▶ Продолжить мою сборку':'▶ Начать сборку')+'</a></p>';
   return '<details class="item cargo-'+state.kind+'" data-cargo="'+esc(p.id)+'" '+(expanded.has(p.id)?'open':'')+'>'+
    '<summary><span class="cargo-title">'+esc(p.destination)+' · '+esc(p.label)+'</span><span class="cargo-badge '+state.kind+'">'+esc(state.label)+'</span></summary>'+
    '<div class="small">ШК: '+esc(p.barcode)+' · Собрано '+esc(p.picked)+' из '+esc(p.qty)+' шт.</div>'+
    '<div class="cargo-progress" role="progressbar" aria-valuenow="'+pct.toFixed(0)+'" aria-valuemin="0" aria-valuemax="100"><div style="width:'+pct.toFixed(1)+'%"></div></div>'+
    action+
    '<div class="cargo-positions">'+ordered.map(i=>
      '<div class="cargo-position '+(i.remaining===0?'position-done':'position-pending')+'">'+
      '<span class="position-icon" aria-hidden="true">'+(i.remaining===0?'✓':'○')+'</span><div class="position-name"><b>'+esc(i.article)+'</b><div class="small">'+esc(i.barcode)+'</div></div>'+
      '<span class="position-count">'+esc(i.picked)+'/'+esc(i.qty)+'</span></div>').join('')+'</div></details>';
  }).join('')+'</div>'
 ).join(''):'<div class="card">Активных заданий Ozon FBO нет.</div>';
}
load().catch(e=>{document.getElementById('list').textContent=e.message});
setInterval(()=>{if(!document.hidden)load().catch(()=>{})},15000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)load().catch(()=>{})});
</script></main></body></html>"""


def _db():
    global _ready
    ensure_db()
    if _ready:
        return
    with _init_lock:
        if _ready:
            return
        ddl = (
            """CREATE TABLE IF NOT EXISTS ozon_fbo_supplies (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_filename TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                plan_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                places INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                lines_count INTEGER NOT NULL,
                warning_count INTEGER NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS ozon_fbo_cargo (
                id TEXT PRIMARY KEY,
                supply_id TEXT NOT NULL,
                barcode TEXT NOT NULL,
                destination TEXT NOT NULL,
                label TEXT NOT NULL,
                planned_qty INTEGER NOT NULL,
                lines_count INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'planned',
                UNIQUE (supply_id, barcode),
                FOREIGN KEY (supply_id) REFERENCES ozon_fbo_supplies(id)
            )""",
            """CREATE TABLE IF NOT EXISTS ozon_fbo_items (
                id TEXT PRIMARY KEY,
                cargo_id TEXT NOT NULL,
                barcode TEXT NOT NULL,
                article TEXT NOT NULL,
                planned_qty INTEGER NOT NULL,
                source_cell TEXT NOT NULL,
                UNIQUE (cargo_id, barcode, article),
                FOREIGN KEY (cargo_id) REFERENCES ozon_fbo_cargo(id)
            )""",
            """CREATE TABLE IF NOT EXISTS ozon_fbo_import_corrections (
                id TEXT PRIMARY KEY, supply_id TEXT NOT NULL,
                cargo_barcode TEXT NOT NULL, article TEXT NOT NULL,
                source_cell TEXT NOT NULL, old_barcode TEXT NOT NULL,
                new_barcode TEXT NOT NULL, actor TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (supply_id) REFERENCES ozon_fbo_supplies(id)
            )""",
            """CREATE TABLE IF NOT EXISTS ozon_fbo_import_warnings (
                id TEXT PRIMARY KEY,
                supply_id TEXT NOT NULL,
                warning TEXT NOT NULL,
                FOREIGN KEY (supply_id) REFERENCES ozon_fbo_supplies(id)
            )""",
            "CREATE INDEX IF NOT EXISTS idx_ozon_fbo_cargo_supply ON ozon_fbo_cargo(supply_id)",
            "CREATE INDEX IF NOT EXISTS idx_ozon_fbo_items_cargo ON ozon_fbo_items(cargo_id)",
        )
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                for statement in ddl:
                    cur.execute(statement)
                conn.commit()
            finally:
                cur.close()
        _ready = True


def _manifest_from_request():
    upload = request.files.get("file")
    if not upload or not (upload.filename or "").lower().endswith(".xlsx"):
        raise ValueError("Загрузи файл в формате .xlsx")
    content = upload.stream.read(8 * 1024 * 1024 + 1)
    parsed = parse_ozon_xlsx(content)
    parsed["source_hash"] = hashlib.sha256(content).hexdigest()
    signature = [
        (p["barcode"], p["destination"], p["label"],
         sorted((i["barcode"], i["article"], i["qty"]) for i in p["items"]))
        for p in parsed["cargo"]
    ]
    parsed["plan_hash"] = hashlib.sha256(
        json.dumps(sorted(signature), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    parsed["filename"] = os.path.basename(upload.filename or "ozon-fbo.xlsx")[:180]
    return parsed


def _response_summary(data):
    return {k: data[k] for k in (
        "cargo", "warnings", "errors", "source_rows", "unique_rows", "qty",
        "places", "destinations", "source_hash", "plan_hash"
    )}


def _list():
    rows = query_all("""
        SELECT id, title, source_filename, status, created_at, created_by,
               places, qty, lines_count, warning_count
        FROM ozon_fbo_supplies ORDER BY created_at DESC LIMIT 60
    """)
    return [{"id": r[0], "title": r[1], "filename": r[2], "status": r[3],
             "created_at": r[4], "created_by": r[5], "places": r[6],
             "qty": r[7], "lines_count": r[8], "warning_count": r[9]}
            for r in rows]


def _details(supply_id):
    header = query_one("""
        SELECT id, title, status, created_at, created_by, places, qty, warning_count
        FROM ozon_fbo_supplies WHERE id = ?
    """, (supply_id,))
    if not header:
        return None
    places = query_all("""
        SELECT id, barcode, destination, label, planned_qty, lines_count, status
        FROM ozon_fbo_cargo WHERE supply_id = ? ORDER BY destination, label, barcode
    """, (supply_id,))
    cargo = []
    for p in places:
        items = query_all("""
            SELECT article, barcode, planned_qty, source_cell FROM ozon_fbo_items
            WHERE cargo_id = ? ORDER BY article, barcode
        """, (p[0],))
        cargo.append({
            "id": p[0], "barcode": p[1], "destination": p[2], "label": p[3],
            "qty": p[4], "lines_count": p[5], "status": p[6],
            "items": [{"article": it[0], "barcode": it[1], "qty": it[2], "source": it[3]}
                      for it in items],
        })
    return {
        "supply": {"id": header[0], "title": header[1], "status": header[2],
                   "created_at": header[3], "created_by": header[4],
                   "places": header[5], "qty": header[6], "warning_count": header[7]},
        "cargo": cargo,
    }


@ozon_fbo_bp.get("/manager/ozon-fbo")
def manager_page():
    if not manager_access():
        return redirect("/manager-login")
    _db()
    return MANAGER_HTML


@ozon_fbo_bp.get("/ozon-fbo")
def worker_page():
    if not get_session_employee():
        return redirect("/login")
    _db()
    return WORKER_HTML


@ozon_fbo_bp.post("/api/manager/ozon-fbo/preview")
def preview():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    _db()
    try:
        data = _manifest_from_request()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    if data["errors"]:
        return jsonify(**_response_summary(data), discrepancies=[])
    try:
        issues = check_catalog(data)
    except CatalogUnavailable as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(**_response_summary(data), discrepancies=issues)


@ozon_fbo_bp.post("/api/manager/ozon-fbo/import")
def import_plan():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    _db()
    try:
        plan = _manifest_from_request()
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    title = str(request.form.get("title") or "").strip()
    if not 2 <= len(title) <= 140:
        return jsonify(error="Название поставки должно содержать 2–140 символов"), 400
    if plan["errors"]:
        return jsonify(error="В Excel обнаружены ошибки. Исправь файл перед импортом.", errors=plan["errors"]), 422
    if (request.form.get("source_hash") != plan["source_hash"] or
            request.form.get("plan_hash") != plan["plan_hash"]):
        return jsonify(error="Файл изменился после предварительной проверки. Проверь Excel снова."), 409
    if plan["warnings"] and request.form.get("accept_duplicates") != "1":
        return jsonify(error="Подтверди исключение повторных строк из плана."), 409
    try:
        decisions = json.loads(request.form.get("catalog_decisions") or "{}")
        excluded = json.loads(request.form.get("excluded_cargo") or "[]")
        issues = check_catalog(plan)
        safe_cargo, corrections = resolved_manifest(plan, issues, decisions, excluded)
    except CatalogUnavailable as exc:
        return jsonify(error=str(exc)), 503
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 409
    final_hash = resolved_plan_hash(safe_cargo)
    duplicate = query_one("""
        SELECT id, title FROM ozon_fbo_supplies
        WHERE source_hash = ? OR plan_hash = ? LIMIT 1
    """, (plan["source_hash"], final_hash))
    if duplicate:
        return jsonify(error="Такая поставка уже загружена: " + duplicate[1], existing_id=duplicate[0]), 409

    sid = str(uuid.uuid4())
    employee = get_session_employee()
    actor = employee["name"] if employee and employee["role"] == "manager" else "Руководитель (ПК)"
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql("""
                INSERT INTO ozon_fbo_supplies
                (id,title,source_filename,source_hash,plan_hash,created_at,created_by,
                 places,qty,lines_count,warning_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """), (sid, title, plan["filename"], plan["source_hash"],
                    final_hash, now_iso(), actor, len(safe_cargo),
                    sum(p["qty"] for p in safe_cargo),
                    sum(len(p["items"]) for p in safe_cargo), len(plan["warnings"])))
            for place in safe_cargo:
                cid = str(uuid.uuid4())
                cur.execute(sql("""
                    INSERT INTO ozon_fbo_cargo
                    (id,supply_id,barcode,destination,label,planned_qty,lines_count)
                    VALUES (?,?,?,?,?,?,?)
                """), (cid, sid, place["barcode"], place["destination"], place["label"],
                        place["qty"], len(place["items"])))
                for item in place["items"]:
                    cur.execute(sql("""
                        INSERT INTO ozon_fbo_items
                        (id,cargo_id,barcode,article,planned_qty,source_cell)
                        VALUES (?,?,?,?,?,?)
                    """), (str(uuid.uuid4()), cid, item["barcode"], item["article"],
                            item["qty"], item["source"]))
            for change in corrections:
                cur.execute(sql("""
                    INSERT INTO ozon_fbo_import_corrections
                    (id,supply_id,cargo_barcode,article,source_cell,old_barcode,
                     new_barcode,actor,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """), (str(uuid.uuid4()),sid,change["cargo"],change["article"],
                      change["source"],change["old"],change["new"],actor,now_iso()))
            for warning in plan["warnings"]:
                cur.execute(sql("""
                    INSERT INTO ozon_fbo_import_warnings (id,supply_id,warning) VALUES (?,?,?)
                """), (str(uuid.uuid4()), sid, warning))
            conn.commit()
        except Exception:
            conn.rollback()
            # Unique constraints still protect against parallel uploads.
            return jsonify(error="Не удалось сохранить план; возможно, поставку уже импортировали. Обнови список."), 409
        finally:
            cur.close()
    return jsonify(ok=True, id=sid, places=len(safe_cargo),
                   qty=sum(p["qty"] for p in safe_cargo),
                   corrections=len(corrections), excluded=len(excluded)), 201


@ozon_fbo_bp.get("/api/manager/ozon-fbo/supplies")
def manager_list():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    _db()
    return jsonify(supplies=_list())


@ozon_fbo_bp.get("/api/manager/ozon-fbo/supplies/<supply_id>")
def manager_detail(supply_id):
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    _db()
    result = _details(supply_id)
    return jsonify(result) if result else (jsonify(error="Поставка не найдена"), 404)


@ozon_fbo_bp.get("/api/ozon-fbo/supplies")
def worker_list():
    if not get_session_employee():
        return jsonify(error="Войди по бейджу"), 401
    _db()
    return jsonify(supplies=_list())


@ozon_fbo_bp.get("/api/ozon-fbo/supplies/<supply_id>")
def worker_detail(supply_id):
    if not get_session_employee():
        return jsonify(error="Войди по бейджу"), 401
    _db()
    result = _details(supply_id)
    return jsonify(result) if result else (jsonify(error="Поставка не найдена"), 404)
