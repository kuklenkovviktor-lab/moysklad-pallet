"""ONIX manager-only schematic warehouse map. Reads replenishment data, never changes stock."""
import re
import uuid
from datetime import datetime, timezone

from flask import Blueprint, jsonify, redirect, request

from wms import db_connect, ensure_db, manager_access, now_iso, query_all, sql

warehouse_map_bp = Blueprint("warehouse_map", __name__)

LINES = tuple("АБВГДЕЖЗИКЛМН")
VALID_LABEL = re.compile(r"^[А-ЯЁA-Z0-9][А-ЯЁA-Z0-9._/-]{0,39}$")
LINE_PATTERN = re.compile(r"^([А-ЯЁA-Z]{1,3})-?\d")
NUM_PATTERN = re.compile(r"(\d+)")
WARN_AFTER_MINUTES = 30


def line_of(label):
    """Infer schematic line for conventional labels (К-07, Ж-01, И4)."""
    text = str(label or "").strip().upper()
    m = LINE_PATTERN.match(text)
    return m.group(1) if m else "ПРОЧЕЕ"


def location_sort_key(location):
    """Order by known warehouse aisle, then numeric cell order."""
    label = location["label"]
    line = location["line"]
    line_order = LINES.index(line) if line in LINES else len(LINES)
    parts = NUM_PATTERN.split(label)
    natural = tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)
    return line_order, line if line_order == len(LINES) else "", natural


def _as_datetime(value):
    if not value:
        return None
    try:
        raw = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return raw.replace(tzinfo=timezone.utc) if raw.tzinfo is None else raw
    except (ValueError, TypeError):
        return None


def cell_state(tasks, last_complete=None, now=None):
    """Task health, not physical stock: never assume green means stock available."""
    now = now or datetime.now(timezone.utc)
    live = [t for t in tasks if t["status"] in ("new", "started")]
    blocked = [t for t in tasks if t["status"] == "blocked"]
    if any(t.get("priority") == "urgent" for t in live):
        return "urgent"
    if live:
        for t in live:
            started = _as_datetime(t.get("started_at") or t.get("created_at"))
            if started and (now - started).total_seconds() > WARN_AFTER_MINUTES * 60:
                return "delayed"
        return "started" if any(t["status"] == "started" for t in live) else "queued"
    if blocked:
        return "blocked"
    if last_complete:
        return "completed"
    return "unknown"


WAREHOUSE_MAP_PAGE = r"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRIT WMS · Карта склада</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e9eef3;color:#25313d;font:14px system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:1500px;margin:0 auto;padding:18px}
a{color:#355e77}h1{font-size:25px;margin:9px 0 6px}
.small{color:#667786;font-size:12px;line-height:1.5}.top{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;flex-wrap:wrap}
button{font:inherit;cursor:pointer}.action{border:0;background:#41637c;color:white;padding:10px 14px;border-radius:9px;font-weight:650}
.action:disabled{opacity:.5}.head,.toolbar,.panel,.details,.register{background:#f9fbfc;border:1px solid #d4e0e9;border-radius:13px;padding:16px;box-shadow:0 2px 8px #37546b0c}
.head{margin-bottom:12px}.toolbar{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin-bottom:12px}
.kpi{background:#eef3f7;padding:11px;border-radius:9px}.kpi b{display:block;font-size:22px}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
input,textarea,select{background:white;border:1px solid #bfceda;border-radius:8px;padding:10px;color:#273b4b;font:inherit}
#search{min-width:220px;flex:1;max-width:410px}
.linebtn{border:1px solid #c8d6e0;background:#fff;color:#32526b;padding:8px 11px;border-radius:9px;font-weight:650}
.linebtn.active{background:#41637c;color:white}
.legend{display:flex;gap:8px 14px;flex-wrap:wrap;align-items:center;margin:10px 0 16px}
.legend span{display:flex;align-items:center;gap:5px;font-size:12px;color:#526778}
.swatch{width:14px;height:14px;display:inline-block;border:1px solid #0002;border-radius:3px}
.zones{display:grid;grid-template-columns:repeat(auto-fit,minmax(315px,1fr));gap:13px;align-items:start}
.panel h2{margin:0 0 11px;font-size:18px}.panel .counts{float:right;font-size:12px;color:#657786;font-weight:400}
.cells{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:5px}
.cell{min-width:0;min-height:40px;border:1px solid #9faab3;border-radius:5px;font-weight:700;font-size:11px;color:#1c3443;padding:4px 1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;position:relative}
.cell:hover{filter:brightness(.96);outline:2px solid #345d7f;outline-offset:0}
.cell.active{outline:3px solid #243c51}
.cell:focus-visible{outline:3px solid #243c51}
.cell .mark{position:absolute;top:0;right:2px;font-size:10px}
.unknown{background:#e5e9ec}.completed{background:#b5e58f}.queued{background:#ffe06f}
.started{background:#95d5f0}.delayed{background:#ffc385}.urgent,.blocked{background:#ff9292}
.bottom{display:grid;grid-template-columns:minmax(0,1.5fr) minmax(280px,1fr);gap:13px;margin-top:14px;align-items:start}
.details{position:sticky;top:14px}.details h2{margin:0 0 8px;font-size:19px}
.detail-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 12px;margin:12px 0}
.detail-grid dt{font-size:11px;color:#637988}.detail-grid dd{margin:2px 0 0;font-weight:650;overflow-wrap:anywhere}
.task{padding:10px 0;border-top:1px solid #e2e9ef}
.task:first-child{border-top:0}.task b{font-size:13px}
.tag{display:inline-block;padding:3px 7px;border-radius:6px;background:#e3edf4;margin:3px 4px 3px 0;font-size:11px}
#notice{min-height:20px;color:#b13c39;margin-top:8px}
.register h2{font-size:17px;margin:0 0 8px}
.register textarea{width:100%;height:96px;resize:vertical}
.register .row{display:flex;gap:8px;align-items:center;justify-content:space-between;margin:8px 0}
#registerMsg{color:#2e7152;font-size:12px;overflow-wrap:anywhere}
.empty{color:#607486;padding:20px;text-align:center;border:1px dashed #aec1cf;border-radius:9px;grid-column:1/-1}
@media(max-width:840px){.toolbar{grid-template-columns:repeat(2,1fr)}.bottom{grid-template-columns:1fr}.details{position:static}}
@media(max-width:360px){.zones{grid-template-columns:minmax(0,1fr)}.cells{grid-template-columns:repeat(5,minmax(0,1fr))}}
</style></head>
<body><div class="wrap">
<header class="head">
 <div class="top"><div><a href="/manager">← Панель руководителя</a> · <a href="/manager/replenishment">Задания пополнения</a> · <a href="/manager/cell-audit">📷 Обход ячеек</a>
 <h1>🗺️ Карта ячеек ONIX WMS</h1>
 <div class="small">Оперативные статусы пополнения по зарегистрированным адресам. Это не карта фактических остатков.</div></div>
 <button class="action" id="reload" type="button">Обновить карту</button></div>
 <div class="small" id="updated">Загрузка…</div><div id="notice" role="alert"></div>
</header>
<div class="toolbar">
 <div class="kpi"><span class="small">Всего ячеек</span><b id="nTotal">—</b></div>
 <div class="kpi"><span class="small">В очереди</span><b id="nQueued">—</b></div>
 <div class="kpi"><span class="small">В работе</span><b id="nStarted">—</b></div>
 <div class="kpi"><span class="small">Проблемные</span><b id="nProblem">—</b></div>
 <div class="kpi"><span class="small">Нет данных</span><b id="nUnknown">—</b></div>
</div>
<div class="filters">
 <label class="small" for="search">Найти ячейку или артикул:</label>
 <input id="search" type="search" placeholder="Например К-07 или SALAT">
 <label><input type="checkbox" id="onlyProblems"> Только проблемные</label>
</div>
<div class="filters" id="lines"></div>
<div class="legend">
 <span><i class="swatch unknown"></i>Нет данных</span>
 <span><i class="swatch completed"></i>Последнее пополнение завершено</span>
 <span><i class="swatch queued"></i>Ожидает исполнителя</span>
 <span><i class="swatch started"></i>В работе</span>
 <span><i class="swatch delayed"></i>Более 30 мин</span>
 <span><i class="swatch urgent"></i>Срочно / блокировка</span>
</div>
<div id="zones" class="zones"><div class="empty">Загрузка реальных ячеек…</div></div>
<div class="bottom">
 <section class="register">
  <h2>Добавить адреса в карту</h2>
  <p class="small">Вставь реальные адреса с этикеток, по одному на строку или через запятую. Существующие ячейки и ШК сохраняются. Новые получают ШК ONIX для последующей печати в модуле пополнения.</p>
  <textarea id="labels" placeholder="К-08&#10;К-09&#10;Ж-02" maxlength="5000"></textarea>
  <div class="row"><span class="small">До 100 адресов за раз</span><button id="add" class="action" type="button">Зарегистрировать</button></div>
  <div id="registerMsg" role="status"></div>
 </section>
 <section class="details" id="details" aria-live="polite">
  <h2>Карточка ячейки</h2><p class="small">Выбери ячейку на карте. Здесь будут задачи, исполнитель, количество, время и журнал последнего пополнения.</p>
 </section>
</div>
</div>
<script>
const STATE_TEXT={
 unknown:'Нет данных о пополнении',completed:'Последнее пополнение выполнено',
 queued:'Ожидает исполнителя',started:'В работе',delayed:'Задержка более 30 минут',
 urgent:'Срочная задача',blocked:'Есть заблокированная задача'
};
let allCells=[],activeLine='all',selectedId='',timer=null;
const esc=(v)=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmt=(d)=>d?new Date(d).toLocaleString('ru-RU'):'—';
function count(id,n){document.getElementById(id).textContent=n;}
function summary(){
 count('nTotal',allCells.length);
 count('nQueued',allCells.filter(c=>c.tasks.some(t=>t.status==='new')).length);
 count('nStarted',allCells.filter(c=>c.tasks.some(t=>t.status==='started')).length);
 count('nProblem',allCells.filter(c=>['urgent','blocked','delayed'].includes(c.state)).length);
 count('nUnknown',allCells.filter(c=>c.state==='unknown').length);
}
function buildLines(){
 const groups=[...new Set(allCells.map(c=>c.line))];
 const root=document.getElementById('lines');
 root.innerHTML=['all',...groups].map(v=>'<button type="button" class="linebtn'+(activeLine===v?' active':'')+'" data-line="'+esc(v)+'">'+esc(v==='all'?'Все линии':v==='ПРОЧЕЕ'?'Прочие':'Линия '+v)+'</button>').join('');
}
function render(){
 const q=document.getElementById('search').value.trim().toLowerCase();
 const problemOnly=document.getElementById('onlyProblems').checked;
 let visible=allCells.filter(c=>(activeLine==='all'||c.line===activeLine) &&
  (!problemOnly||['queued','started','delayed','urgent','blocked'].includes(c.state)) &&
  (!q||[c.label,c.assignment?.article||'',c.last_task?.article||'',...(c.tasks||[]).map(t=>t.article)].some(v=>String(v).toLowerCase().includes(q))));
 const grouped={};for(const c of visible)(grouped[c.line]??=[]).push(c);
 const root=document.getElementById('zones');
 root.innerHTML=Object.entries(grouped).map(([line,cells])=>
  '<section class="panel"><h2>'+esc(line==='ПРОЧЕЕ'?'Другие адреса':'Линия '+line)+
  '<span class="counts">'+cells.length+' ячеек</span></h2><div class="cells">'+
  cells.map(c=>'<button type="button" class="cell '+esc(c.state)+(c.id===selectedId?' active':'')+
   '" data-id="'+esc(c.id)+'" title="'+esc(c.label+' · '+STATE_TEXT[c.state])+'" aria-label="'+esc(c.label+' · '+STATE_TEXT[c.state])+'">'+
   esc(c.label)+(c.assignment?'<span class="mark">✓</span>':c.tasks.length?'<span class="mark">'+c.tasks.length+'</span>':'')+'</button>').join('')+
  '</div></section>').join('')||'<div class="empty">Под выбранный фильтр ячеек нет. Добавить реальные адреса можно ниже.</div>';
 showDetails();
}
function taskCard(t){
 let title='<b>'+esc(t.article)+'</b> · '+esc(t.product_name||'Без названия');
 const extras='<div class="small">План: '+esc(t.planned_qty??'—')+' шт. · Факт: '+esc(t.completed_qty??'—')+' шт.</div>'+
  '<div class="small">Исполнитель: '+esc(t.assignee||'Не назначен')+'</div>'+
  '<div class="small">Создано: '+esc(fmt(t.created_at))+' · Начато: '+esc(fmt(t.started_at))+'</div>'+
  (t.note?'<div class="small">Примечание: '+esc(t.note)+'</div>':'');
 return '<div class="task">'+title+'<div><span class="tag">'+esc(t.status)+
 '</span>'+(t.priority==='urgent'?'<span class="tag">СРОЧНО</span>':'')+'</div>'+extras+'</div>';
}
function showDetails(){
 const c=allCells.find(x=>x.id===selectedId);
 const el=document.getElementById('details');
 if(!c){el.innerHTML='<h2>Карточка ячейки</h2><p class="small">Нажми на ячейку, чтобы увидеть задания и последнее пополнение.</p>';return}
 el.innerHTML='<h2>'+esc(c.label)+'</h2><span class="tag '+esc(c.state)+'">'+esc(STATE_TEXT[c.state])+'</span>'+
 '<dl class="detail-grid"><div><dt>Линия</dt><dd>'+esc(c.line)+'</dd></div>'+
 '<div><dt>Активные задачи</dt><dd>'+c.tasks.length+'</dd></div>'+
 '<div><dt>Последняя проверка товара</dt><dd>'+esc(c.last_task?.verified_product_at?'Да':'Нет данных')+'</dd></div>'+
 '<div><dt>Последняя проверка ячейки</dt><dd>'+esc(c.last_task?.verified_location_at?'Да':'Нет данных')+'</dd></div></dl>'+
 '<h3>Постоянная привязка товара</h3>'+(c.assignment?'<div class="info"><b>'+esc(c.assignment.article)+'</b><div class="small">'+esc(c.assignment.name||'')+'</div><div class="small">Закреплено: '+esc(fmt(c.assignment.assigned_at))+'</div></div>':'<p class="small">Не задана. Последнее пополнение не назначает SKU автоматически.</p>')+
 '<p><a href="/manager/cell-audit?location_id='+encodeURIComponent(c.id)+'">📷 Сканировать товар и привязать к ячейке →</a></p>'+
 '<div class="manage-controls"><h3>Управление ячейкой</h3>'+
 '<p class="small">QR постоянный. Освобождение не списывает остатки в МойСклад. Удаление убирает адрес из активной карты, сохраняя историю.</p>'+
 '<div class="actions"><button type="button" data-manage="release" '+(!c.assignment?'disabled':'')+'>Освободить от SKU</button>'+
 '<button type="button" data-manage="rename">Изменить адрес</button>'+
 '<button type="button" data-manage="archive">Удалить ячейку</button></div></div>'+ 
 '<h3>Текущие задачи</h3>'+(c.tasks.length?c.tasks.map(taskCard).join(''):'<p class="small">Нет открытых задач.</p>')+
 '<h3>Последнее завершённое пополнение</h3>'+(c.last_task?taskCard(c.last_task):'<p class="small">В истории пока нет завершённых пополнений.</p>')+
 '<p class="small">Статус показывает работу по пополнению, а не фактический остаток товара в ячейке.</p>'+
 '<a href="/manager/replenishment">Управлять заданиями →</a>';
}
async function fetchData(){
 const btn=document.getElementById('reload');btn.disabled=true;
 try{
  const response=await fetch('/api/manager/warehouse-map',{cache:'no-store'});
  if(response.status===403){location.href='/manager-login';return}
  const d=await response.json();
  if(!response.ok)throw Error(d.error||'Не удалось загрузить карту');
  allCells=d.cells||[];
  document.getElementById('notice').textContent='';
  document.getElementById('updated').textContent='Обновлено: '+new Date().toLocaleTimeString('ru-RU')+' · адресов: '+allCells.length+' · автообновление 15 сек';
  if(selectedId&&!allCells.some(c=>c.id===selectedId))selectedId='';
  if(activeLine!=='all'&&!allCells.some(c=>c.line===activeLine))activeLine='all';
  summary();buildLines();render();
 }catch(e){document.getElementById('notice').textContent=e.message}
 finally{btn.disabled=false}
}
document.getElementById('details').addEventListener('click',async e=>{
 const button=e.target.closest('button[data-manage]');if(!button)return;
 const cell=allCells.find(x=>x.id===selectedId);if(!cell)return;
 const action=button.dataset.manage;
 let newLabel='',confirmedEmpty=false;
 if(action==='rename'){
  const v=prompt('Новый адрес для '+cell.label+'. QR-код сохранится, но напечатанную этикетку нужно заменить:',cell.label);
  if(v===null)return;newLabel=v.trim().toUpperCase();
  if(!newLabel||newLabel===cell.label)return;
 }else if(action==='release'){
  if(!cell.assignment)return;
  confirmedEmpty=confirm('Подтверждаешь, что '+cell.label+' ФИЗИЧЕСКИ ПУСТА?\\nПривязка '+cell.assignment.article+' будет снята. Это НЕ списание остатков.');
  if(!confirmedEmpty)return;
 }
 if(action==='archive'&&!confirm('Убрать '+cell.label+' из активной карты? Нельзя архивировать занятую ячейку или место с открытыми задачами.'))return;
 if(!confirm('Подтвердить операцию '+action+' для '+cell.label+'?'))return;
 button.disabled=true;
 try{
  const r=await fetch('/api/manager/warehouse-map/location/manage',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({location_id:cell.id,expected_label:cell.label,action,
    new_label:newLabel,confirmed_empty:confirmedEmpty,confirmed:true})
  });
  const d=await r.json();if(!r.ok)throw Error(d.error||'Не удалось сохранить');
  await fetchData();
  if(d.reprint_label)alert('Адрес изменён, QR сохранён. Перепечатай этикетку 58×40 мм и замени старую на стеллаже.');
 }catch(err){alert(err.message);button.disabled=false}
});
document.getElementById('zones').addEventListener('click',e=>{
 const btn=e.target.closest('button[data-id]');
 if(!btn)return;
 selectedId=btn.dataset.id;
 render();
});
document.getElementById('lines').addEventListener('click',e=>{
 const btn=e.target.closest('button[data-line]');
 if(!btn)return;activeLine=btn.dataset.line;buildLines();render();
});
document.getElementById('search').addEventListener('input',render);
document.getElementById('onlyProblems').addEventListener('change',render);
document.getElementById('reload').addEventListener('click',fetchData);
document.getElementById('add').addEventListener('click',async ()=>{
 const btn=document.getElementById('add'),out=document.getElementById('registerMsg');
 const labels=document.getElementById('labels').value.split(/[\n,;]+/).map(x=>x.trim()).filter(Boolean);
 if(!labels.length){out.textContent='Укажи реальные адреса';return}
 btn.disabled=true;out.textContent='';
 try{
  const r=await fetch('/api/manager/warehouse-map/locations',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({labels})});
  const d=await r.json();
  if(!r.ok)throw Error(d.error||'Не удалось добавить адреса');
  out.textContent='Добавлено: '+d.created.length+'. Уже было: '+d.existing.length+'. ШК новых адресов доступны для печати в модуле пополнения.';
  document.getElementById('labels').value='';
  await fetchData();
 }catch(e){out.textContent=e.message}finally{btn.disabled=false}
});
fetchData();
timer=setInterval(()=>{if(!document.hidden)fetchData()},15000);
</script></body></html>"""


def _task(row):
    keys = ("id", "article", "product_name", "status", "priority", "planned_qty",
            "completed_qty", "created_at", "started_at", "completed_at", "assignee",
            "note", "verified_product_at", "verified_location_at", "line")
    return dict(zip(keys, row))


TASK_COLUMNS = """
 SELECT t.id, t.article, t.product_name, t.status, t.priority,
        t.planned_qty, t.completed_qty, t.created_at, t.started_at,
        t.completed_at, COALESCE(e.name, ''), t.note,
        t.verified_product_at, t.verified_location_at, t.line_code
 FROM replenishment_tasks t LEFT JOIN employees e ON e.id=t.assigned_employee_id
"""


def warehouse_map_snapshot():
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    assignment_rows = query_all("""
        SELECT location_id, product_id, article, product_name, assigned_at, assigned_by
        FROM cell_sku_assignments
    """)
    assignments = {r[0]:{"product_id":r[1],"article":r[2],"name":r[3],"assigned_at":r[4],"assigned_by":r[5]} for r in assignment_rows}
    locations = query_all("""
      SELECT id, label, barcode FROM replenishment_locations
      WHERE active = 1 ORDER BY label LIMIT 5000
    """)
    # Keep active tasks per cell and one latest completed task. Do not use
    # an old completed task as proof of current positive stock.
    active = query_all(TASK_COLUMNS + """
      WHERE t.status IN ('new','started','blocked')
      ORDER BY t.created_at DESC LIMIT 5000
    """)
    completed = query_all(TASK_COLUMNS + """
      WHERE t.status='completed'
      ORDER BY t.completed_at DESC LIMIT 5000
    """)
    by_line = {}
    for row in active:
        task = _task(row)
        by_line.setdefault(task["line"], []).append(task)
    by_completed = {}
    for row in completed:
        task = _task(row)
        by_completed.setdefault(task["line"], task)
    now = datetime.now(timezone.utc)
    cells = []
    for loc_id, label, barcode in locations:
        tasks = by_line.get(label, [])
        last = by_completed.get(label)
        record = {"id": loc_id, "label": label, "line": line_of(label),
                  "state": cell_state(tasks, last, now),
                  "tasks": tasks, "last_task": last, "assignment": assignments.get(loc_id)}
        cells.append(record)
    cells.sort(key=location_sort_key)
    return cells


@warehouse_map_bp.get("/manager/warehouse-map")
def warehouse_map_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return WAREHOUSE_MAP_PAGE


@warehouse_map_bp.get("/api/manager/warehouse-map")
def warehouse_map_data():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    return jsonify(cells=warehouse_map_snapshot(), stock_is_authoritative=False,
                   delayed_after_minutes=WARN_AFTER_MINUTES)


@warehouse_map_bp.post("/api/manager/warehouse-map/locations")
def warehouse_map_bulk_locations():
    """Register only explicitly supplied real addresses, preserving existing barcodes."""
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    if request.content_length and request.content_length > 20000:
        return jsonify(error="Слишком длинный список адресов"), 413
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict) or not isinstance(body.get("labels"), list):
        return jsonify(error="Укажи список адресов"), 400
    requested = body["labels"]
    if not 1 <= len(requested) <= 100 or any(not isinstance(x, str) for x in requested):
        return jsonify(error="За один раз можно загрузить от 1 до 100 адресов"), 400
    normalized = [x.strip().upper() for x in requested]
    if any(not VALID_LABEL.fullmatch(x) for x in normalized):
        return jsonify(error="Некорректный адрес: используй буквы, цифры и символы - / . _"), 400
    if len(normalized) != len(set(normalized)):
        return jsonify(error="В списке есть повторяющиеся адреса"), 409
    existing = []
    created = []
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            for label in normalized:
                cur.execute(sql("SELECT id FROM replenishment_locations WHERE label = ?"), (label,))
                found = cur.fetchone()
                if found:
                    existing.append(label)
                    continue
                loc_id = str(uuid.uuid4())
                barcode = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
                cur.execute(sql("""
                  INSERT INTO replenishment_locations
                    (id, label, barcode, created_at, active) VALUES (?, ?, ?, ?, 1)
                """), (loc_id, label, barcode, now_iso()))
                created.append({"id": loc_id, "label": label})
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True, created=created, existing=existing), 201


@warehouse_map_bp.post("/api/manager/warehouse-map/location/manage")
def warehouse_map_manage_location():
    """Edit a real location without silently changing warehouse stock or task history."""
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    body = request.get_json(silent=True) if request.is_json else None
    if not isinstance(body, dict):
        return jsonify(error="Ожидается JSON"), 400
    loc_id = str(body.get("location_id") or "").strip()
    action = str(body.get("action") or "").strip()
    expected = str(body.get("expected_label") or "").strip().upper()
    if not loc_id or action not in ("rename", "release", "archive") or not expected:
        return jsonify(error="Укажи ячейку, действие и текущий адрес"), 400
    if body.get("confirmed") is not True:
        return jsonify(error="Требуется подтверждение операции"), 400
    new_label = str(body.get("new_label") or "").strip().upper()
    if action == "rename" and not VALID_LABEL.fullmatch(new_label):
        return jsonify(error="Некорректный новый адрес"), 400
    if action == "release" and body.get("confirmed_empty") is not True:
        return jsonify(error="Сначала физически проверь отсутствие товара в ячейке"), 400
    from wms import DATABASE_URL, now_iso
    import uuid
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            lock = " FOR UPDATE" if DATABASE_URL else ""
            cur.execute(sql("SELECT label, barcode, active FROM replenishment_locations WHERE id=?" + lock), (loc_id,))
            row = cur.fetchone()
            if not row or not row[2]:
                return jsonify(error="Активная ячейка не найдена"), 404
            old_label, barcode = row[0], row[1]
            if old_label != expected:
                return jsonify(error="Адрес изменился. Обнови карту"), 409
            cur.execute(sql("SELECT product_id, article FROM cell_sku_assignments WHERE location_id=?" + lock), (loc_id,))
            assignment = cur.fetchone()
            cur.execute(sql("""
                SELECT COUNT(*) FROM replenishment_tasks
                WHERE line_code=? AND status IN ('new','started','blocked')
            """), (old_label,))
            pending = cur.fetchone()[0]
            if pending:
                return jsonify(error="По ячейке есть незавершённые задачи пополнения. Сначала закрой или отмени их"), 409
            if action == "rename":
                if new_label == old_label:
                    return jsonify(ok=True, unchanged=True, label=old_label, barcode=barcode)
                cur.execute(sql("SELECT COUNT(*) FROM replenishment_locations WHERE label=?"), (new_label,))
                if cur.fetchone()[0]:
                    return jsonify(error="Такой адрес уже существует, включая архивные"), 409
                # Replenishment tasks reference the current address as text, not
                # location_id. Rebind the closed task history atomically, while
                # retaining the original task IDs and a durable rename audit.
                # Open tasks have already been rejected above.
                cur.execute(sql("""
                    CREATE TABLE IF NOT EXISTS replenishment_location_rename_events (
                        id TEXT PRIMARY KEY,
                        location_id TEXT NOT NULL,
                        old_label TEXT NOT NULL,
                        new_label TEXT NOT NULL,
                        changed_at TEXT NOT NULL,
                        actor TEXT NOT NULL
                    )
                """))
                cur.execute(sql("""
                    UPDATE replenishment_tasks SET line_code=?
                    WHERE line_code=? AND status NOT IN ('new','started','blocked')
                """), (new_label, old_label))
                cur.execute(sql("""
                    INSERT INTO replenishment_location_rename_events
                    (id,location_id,old_label,new_label,changed_at,actor)
                    VALUES (?,?,?,?,?,?)
                """), (str(uuid.uuid4()), loc_id, old_label, new_label,
                      now_iso(), "Руководитель"))
                cur.execute(sql("UPDATE replenishment_locations SET label=? WHERE id=? AND active=1"), (new_label, loc_id))
                result = new_label
            elif action == "release":
                if not assignment:
                    return jsonify(ok=True, unchanged=True, label=old_label, barcode=barcode)
                cur.execute(sql("DELETE FROM cell_sku_assignments WHERE location_id=?"), (loc_id,))
                cur.execute(sql("""
                    INSERT INTO cell_sku_assignment_events
                    (id,location_id,old_product_id,old_article,new_product_id,new_article,kind,actor,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """), (str(uuid.uuid4()), loc_id, assignment[0], assignment[1],
                      None, None, "release", "Руководитель", now_iso()))
                result = old_label
            else:
                if assignment:
                    return jsonify(error="Сначала освободи ячейку от товара и подтверди, что она пуста"), 409
                cur.execute(sql("UPDATE replenishment_locations SET active=0 WHERE id=? AND active=1"), (loc_id,))
                result = old_label
            conn.commit()
        except Exception:
            conn.rollback()
            return jsonify(error="Операция не сохранена. Проверь адрес и повтори"), 409
        finally:
            cur.close()
    return jsonify(ok=True, action=action, label=result, barcode=barcode,
                   stock_changed=False, reprint_label=(action=="rename"))
