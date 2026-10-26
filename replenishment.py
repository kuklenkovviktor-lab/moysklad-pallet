"""ONIX WMS: independent replenishment task queue. No inventory or scanning mutations."""
import uuid
from flask import Blueprint, jsonify, redirect, request
from wms import db_connect, ensure_db, get_session_employee, manager_access, now_iso, query_all, query_one, sql, DATABASE_URL

replenishment_bp = Blueprint("replenishment", __name__)

TASK_SELECT = """
SELECT t.id, t.article, t.line_code, t.planned_qty, t.completed_qty,
       t.priority, t.note, t.status, t.created_at, t.started_at, t.completed_at,
       t.created_by, t.assigned_employee_id, COALESCE(e.name, ''),
       t.product_id, t.product_name, t.verify_required,
       t.verified_product_at, t.verified_location_at,
       (SELECT ev.note FROM replenishment_task_events ev
        WHERE ev.task_id = t.id AND ev.kind = 'block'
        ORDER BY ev.created_at DESC LIMIT 1)
FROM replenishment_tasks t
LEFT JOIN employees e ON e.id = t.assigned_employee_id
"""


def _item(row):
    fields = ("id", "article", "line", "planned_qty", "completed_qty",
              "priority", "note", "status", "created_at", "started_at",
              "completed_at", "created_by", "assigned_employee_id", "assignee",
              "product_id", "product_name", "verify_required",
              "verified_product_at", "verified_location_at", "last_issue")
    return dict(zip(fields, row))


def _json_required():
    if not request.is_json:
        return None
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else None


def _event(cur, task_id, actor_id, actor, kind, note="", qty=None):
    cur.execute(sql("""
        INSERT INTO replenishment_task_events
        (id, task_id, actor_id, actor, kind, note, quantity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """), (str(uuid.uuid4()), task_id, actor_id, actor, kind, note, qty, now_iso()))


def _check_destination(cur, line, product_id, article):
    """Prevent replenishing a mapped cell with another SKU.

    An unassigned physical cell is still valid: registration and stock are
    distinct concepts. Binding stays a separate manager action.
    The caller must initialize cell_audit tables before entering a DB tx.
    """
    query = """
        SELECT l.id, a.product_id, a.article
        FROM replenishment_locations l
        LEFT JOIN cell_sku_assignments a ON a.location_id = l.id
        WHERE l.label = ? AND l.active = 1
    """
    if DATABASE_URL:
        query += " FOR UPDATE OF l"
    cur.execute(sql(query), (line,))
    found = cur.fetchone()
    if not found:
        return "Ячейка не зарегистрирована или выведена из эксплуатации"
    mapped_id, mapped_article = found[1], found[2]
    if mapped_id and (
        (product_id and str(mapped_id) != str(product_id))
        or str(mapped_article or "").strip().casefold() != str(article or "").strip().casefold()
    ):
        return (
            "НЕ ТОТ ТОВАР ДЛЯ ЯЧЕЙКИ: " + line
            + " закреплена за " + str(mapped_article)
            + ", выбран " + str(article)
            + ". Проверь карту склада и привязку артикула."
        )
    return None


def _open_location_task(cur, line, exclude_id=None):
    statement = """
        SELECT id, article FROM replenishment_tasks
        WHERE line_code = ? AND status IN ('new','started')
    """
    parameters = (line,)
    if exclude_id:
        statement += " AND id <> ?"
        parameters += (exclude_id,)
    statement += " ORDER BY created_at LIMIT 1"
    cur.execute(sql(statement), parameters)
    return cur.fetchone()


MANAGER_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ONIX · Пополнение: руководитель</title><style>
*{box-sizing:border-box}body{font-family:system-ui,Arial;background:#e9eef3;color:#25313d;margin:0}
main{max-width:1100px;margin:auto;padding:16px}h1{margin:12px 0}h2{font-size:18px;margin:0 0 12px}
a{color:#49667b}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:14px;padding:16px;margin:12px 0}
form{display:grid;grid-template-columns:1.1fr .75fr .6fr .6fr;gap:9px;align-items:end}
label{font-size:12px;color:#657683;display:grid;gap:4px}input,select,textarea{width:100%;padding:10px;border:1px solid #cbd8e1;border-radius:8px;background:#fff;font:inherit;color:#25313d}
button{padding:10px 13px;border:0;border-radius:9px;background:#48647a;color:#fff;font-weight:700;cursor:pointer}
button:disabled{opacity:.5}.secondary{background:#dbe7ed;color:#314657}
.wide{grid-column:1/-1}.small{font-size:12px;color:#667989}.item{padding:12px 0;border-top:1px solid #dce4e9;display:flex;align-items:center;justify-content:space-between;gap:12px}
.item:first-child{border-top:0}.detail{min-width:0}.detail>div{margin-top:5px}.urgent{color:#a14c28}.bad{color:#a33b3b}.ok{color:#26764b}
.matches{display:grid;gap:3px;max-height:240px;overflow:auto;margin:8px 0}.matches button{background:#e3ebf2;color:#233c4c;text-align:left}
#msg{min-height:18px;margin-top:8px}@media(max-width:730px){form{grid-template-columns:1fr 1fr}.item{align-items:flex-start;flex-direction:column}}
</style></head><body><main>
<a href="/manager">← Панель руководителя</a> · <a href="/manager/warehouse-map">🗺️ Карта склада</a><h1>📦 Пополнение ячеек</h1>
<p class="small">Очередь пополнения. Задания создают руководитель и сами сборщики. МойСклад и учёт FBS не изменяются.</p>
<section class="card"><h2>Создать задачу</h2>
<label>Найти товар в МойСклад по названию, артикулу или ШК
<input id="productSearch" type="search" placeholder="Поиск номенклатуры" autocomplete="off"></label>
<div id="productMatches" class="matches"></div>
<form id="newTask">
<label>Выбранный артикул<input name="article" id="chosenArticle" readonly required placeholder="Выбери товар">
<input type="hidden" name="product_id" id="chosenProductId"></label>
<label>Ячейка назначения<select name="line" id="targetLocation" required><option value="">Зарегистрируй адрес ниже</option></select></label>
<label>Количество, шт.<input name="planned_qty" type="number" min="1" step="1" placeholder="Если известно"></label>
<label>Приоритет<select name="priority"><option value="normal">Обычный</option><option value="urgent">Срочно</option></select></label>
<label class="wide">Примечание<input name="note" maxlength="500" placeholder="Откуда взять резерв, детали задачи"></label>
<div><button type="submit" id="createBtn">Добавить в очередь</button></div>
</form><div id="msg" role="status"></div></section>
<section class="card"><h2>Этикетки ячеек</h2>
<p class="small">Добавь линию или адрес (например И или И4) и распечатай штрихкод для сканирования при размещении.</p>
<div style="display:flex;gap:8px;margin:10px 0"><input id="newLocation" maxlength="80" placeholder="Название, например И4" style="flex:1">
<button id="addLocation" type="button">Создать ячейку</button></div>
<div id="locationMsg" class="small" role="status"></div>
<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:13px 0 8px">
<a href="/manager/replenishment/labels/all" target="_blank" rel="noopener"
   id="printAllLocations" style="display:inline-block;padding:11px 16px;background:#48647a;color:white;
    border-radius:9px;text-decoration:none;font-weight:750">🖨 Печать всех ШК на ленте 58×40</a>
<span class="small" id="printAllCount">Загружаем количество ячеек…</span></div>
<p class="small">Все зарегистрированные ячейки одним заданием на термопринтере: одна этикетка 58×40 мм на одну ячейку. Индивидуальная печать остаётся доступной ниже.</p>
<div id="locations">Загрузка…</div></section>
<section class="card"><h2>Открытые задачи <span id="openCount" class="small"></span></h2><div id="queue">Загрузка…</div></section>
<section class="card"><h2>Последние завершённые и проблемные</h2><div id="history">Загрузка…</div></section>
<section class="card"><h2>Журнал действий</h2><div id="events">Загрузка…</div></section>
</main><script>
function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
async function readApiJson(r){
 const body=await r.text();
 try{return JSON.parse(body)}catch(_e){throw Error('Сервер вернул не JSON. Обнови страницу и повтори действие.')}
}
function fmt(s){return s?new Date(s).toLocaleString('ru-RU'):'—'}
function desc(t){
 return '<b>'+esc(t.article)+'</b> · линия '+esc(t.line)+
 (t.product_name?' · '+esc(t.product_name):'')+
 (t.verify_required?'<div class="small">ШК товара: '+(t.verified_product_at?'✅':'ожидается')+
 ' · ШК ячейки: '+(t.verified_location_at?'✅':'ожидается')+'</div>':'<div class="small">Задача без двойной проверки</div>')+
 (t.priority==='urgent'?' · <b class="urgent">СРОЧНО</b>':'')+
 '<div class="small">'+(t.planned_qty?'План: '+t.planned_qty+' шт. · ':'')+'Статус: '+esc(t.status)+
 (t.assignee?' · '+esc(t.assignee):' · свободна')+' · '+fmt(t.created_at)+'</div>'+
 (t.note?'<div class="small">'+esc(t.note)+'</div>':'')+
 (t.completed_qty!=null?'<div class="small">Фактически: '+esc(t.completed_qty)+' шт.</div>':'')+
 (t.started_at&&t.completed_at?'<div class="small">Время выполнения: '+Math.max(0,Math.round((new Date(t.completed_at)-new Date(t.started_at))/60000))+' мин</div>':'')+
 (t.status==='blocked'&&t.last_issue?'<div class="bad">Причина: '+esc(t.last_issue)+'</div>':'');
}

let selectedCatalog=[],searchDebounce=0,locations=[];
function escLoc(x){return esc(x)}
function paintLocations(){
 const count=document.getElementById('printAllCount');
 const bulk=document.getElementById('printAllLocations');
 if(count)count.textContent='Активных ячеек: '+locations.length+' · '+locations.length+' этикеток 58×40 мм';
 if(bulk){bulk.style.pointerEvents=locations.length?'auto':'none';bulk.style.opacity=locations.length?'1':'.5'}
 const sel=document.getElementById('targetLocation'),old=sel.value;
 sel.innerHTML='<option value="">Выбери зарегистрированную ячейку</option>'+locations.map(l=>
 '<option value="'+esc(l.label)+'">'+esc(l.label)+'</option>').join('');
 if(locations.some(l=>l.label===old))sel.value=old;
 document.getElementById('locations').innerHTML=locations.length?locations.map(l=>
 '<div class="item"><div><b>'+esc(l.label)+'</b><div class="small">'+esc(l.barcode)+'</div></div>'+
 '<a href="/manager/replenishment/label?id='+encodeURIComponent(l.id)+'" target="_blank" rel="noopener">🖨 Этикетка</a></div>').join(''):'<p class="small">Ячеек пока нет</p>';
}
async function reloadLocations(){
 const r=await fetch('/api/manager/replenishment/locations',{cache:'no-store'}),d=await readApiJson(r);
 if(!r.ok)throw Error(d.error||'Ошибка загрузки адресов');
 locations=d.locations||[];paintLocations();
}
document.getElementById('addLocation').addEventListener('click',async function(){
 const input=document.getElementById('newLocation'),msg=document.getElementById('locationMsg');
 if(!input.value.trim())return;this.disabled=true;
 try{
  const r=await fetch('/api/manager/replenishment/locations',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({label:input.value.trim()})}),d=await readApiJson(r);
  if(!r.ok)throw Error(d.error||'Ошибка регистрации');
  await reloadLocations();document.getElementById('targetLocation').value=d.location.label;
  msg.textContent='Адрес зарегистрирован. Распечатай его ШК.';
  input.value='';
 }catch(e){msg.textContent=e.message}finally{this.disabled=false}
});
document.getElementById('productSearch').addEventListener('input',function(){
 clearTimeout(searchDebounce);const text=this.value.trim(),box=document.getElementById('productMatches');
 document.getElementById('chosenArticle').value='';document.getElementById('chosenProductId').value='';
 if(text.length<2){box.innerHTML='';return}
 searchDebounce=setTimeout(async function(){
  try{
   const r=await fetch('/api/manager/replenishment/catalog?q='+encodeURIComponent(text)),d=await readApiJson(r);
   if(!r.ok)throw Error(d.error||'Ошибка поиска');
   if(document.getElementById('productSearch').value.trim()!==text)return;
   selectedCatalog=d.products||[];
   box.innerHTML=selectedCatalog.length?selectedCatalog.map((p,i)=>
    '<button type="button" data-index="'+i+'"><b>'+esc(p.article)+'</b> · '+esc(p.name)+'</button>').join(''):
    '<p class="small">Не найдено</p>';
  }catch(e){box.textContent=e.message}
 },350);
});
document.getElementById('productMatches').addEventListener('click',function(e){
 const b=e.target.closest('button[data-index]');if(!b)return;
 const p=selectedCatalog[Number(b.dataset.index)];if(!p)return;
 document.getElementById('chosenArticle').value=p.article;
 document.getElementById('chosenProductId').value=p.id;
 document.getElementById('productSearch').value=p.article+' · '+p.name;
 this.textContent='Выбрано: '+p.article+' · '+p.name;
});
reloadLocations().catch(e=>{document.getElementById('locationMsg').textContent=e.message});
let tasks=[];
async function getTasks(){
 const r=await fetch('/api/manager/replenishment/tasks',{cache:'no-store'});
 if(r.status===403){location.href='/manager-login';return}
 const d=await readApiJson(r);if(!r.ok)throw Error(d.error||'Ошибка загрузки');
 tasks=d.tasks||[];
 const open=tasks.filter(function(t){return t.status==='new'||t.status==='started'});
 const old=tasks.filter(function(t){return t.status!=='new'&&t.status!=='started'});
 document.getElementById('openCount').textContent='('+open.length+')';
 document.getElementById('queue').innerHTML=open.length?open.map(function(t){
  return '<div class="item"><div class="detail">'+desc(t)+'</div><div style="display:flex;gap:6px">'+
 (!t.verify_required&&t.status==='new'?'<button class="secondary" data-id="'+esc(t.id)+'" data-action="enable_checks">Включить сканирование</button>':'')+
 '<button class="secondary" data-id="'+esc(t.id)+'" data-action="cancel">Отменить</button></div></div>'
 }).join(''):'<p class="small">Открытых задач нет.</p>';
 document.getElementById('history').innerHTML=old.length?old.slice(0,35).map(function(t){
  return '<div class="item"><div class="detail">'+desc(t)+'</div>'+
  (t.status==='blocked'?'<button class="secondary" data-id="'+esc(t.id)+'" data-action="reopen">Вернуть в очередь</button>':'')+'</div>'
 }).join(''):'<p class="small">История пока пуста.</p>';
 const events=d.events||[];
 document.getElementById('events').innerHTML=events.length?events.map(function(v){
  return '<div class="item"><div class="detail"><b>'+esc(v.article)+'</b> · '+esc(v.line)+
   ' · '+esc(v.kind)+'<div class="small">'+fmt(v.created_at)+' · '+esc(v.actor)+
   (v.quantity!=null?' · '+esc(v.quantity)+' шт.':'')+'</div>'+
   (v.note?'<div class="small">'+esc(v.note)+'</div>':'')+'</div></div>'
 }).join(''):'<p class="small">Событий пока нет.</p>';
}
document.getElementById('newTask').addEventListener('submit',async function(e){
 e.preventDefault();const b=document.getElementById('createBtn'),m=document.getElementById('msg');
 b.disabled=true;m.textContent='';
 try{
  const f=new FormData(this);
  if(!f.get('product_id'))throw Error('Сначала выбери товар из поиска');
  if(!f.get('line'))throw Error('Сначала выбери ячейку');
  const qty=f.get('planned_qty');
  const r=await fetch('/api/manager/replenishment/create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
   article:f.get('article'),product_id:f.get('product_id'),line:f.get('line'),planned_qty:qty?Number(qty):null,priority:f.get('priority'),note:f.get('note')})});
  const d=await readApiJson(r);
  if(!r.ok)throw Error(d.error||'Ошибка создания');
  this.reset();document.getElementById('productSearch').value='';document.getElementById('productMatches').innerHTML='';
  m.className='ok';m.textContent='Задача добавлена';await getTasks();
 }catch(e){m.className='bad';m.textContent=e.message}finally{b.disabled=false}
});
async function change(e){
 const b=e.target.closest('button[data-action]');if(!b)return;
 const action=b.dataset.action,task=tasks.find(function(t){return t.id===b.dataset.id});if(!task)return;
 const note=prompt(action==='cancel'?'Причина отмены':action==='reopen'?'Причина повторного открытия':'Причина включения двойного сканирования');
 if(note===null)return;
 if(note.trim().length<5){alert('Укажи причину хотя бы из 5 символов');return}
 if(!confirm((action==='cancel'?'Отменить':action==='reopen'?'Вернуть в очередь':'Включить сканирование для')+' задачу '+task.article+'?'))return;
 b.disabled=true;
 try{
  const r=await fetch('/api/manager/replenishment/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:task.id,action:action,note:note})});
  const d=await readApiJson(r);if(!r.ok)throw Error(d.error||'Ошибка');
  await getTasks();
 }catch(e){alert(e.message);b.disabled=false}
}
document.getElementById('queue').addEventListener('click',change);
document.getElementById('history').addEventListener('click',change);
getTasks().catch(function(e){document.getElementById('queue').textContent=e.message});
</script></body></html>"""


WORKER_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ONIX · Пополнение</title><script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script><style>
*{box-sizing:border-box}body{font-family:system-ui,Arial;background:#e9eef3;color:#25313d;margin:0}
main{max-width:760px;margin:auto;padding:13px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:14px;padding:14px;margin:10px 0}
h1{margin:12px 0}a{color:#49667b}.small{color:#667989;font-size:12px}.task{padding:14px 0;border-top:1px solid #d7e0e7}.task:first-child{border-top:0}
.task b{font-size:18px}.urgent{color:#a14c28}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
input,select{width:125px;padding:11px;border:1px solid #b8cbd5;border-radius:8px;font-size:16px}
button{background:#48647a;color:white;border:0;padding:11px 13px;border-radius:9px;font-weight:700;cursor:pointer;min-height:42px}
.secondary{background:#dce7ed;color:#25313d}button:disabled{opacity:.5}.bad{color:#a33b3b}
.ok{color:#26764b;font-weight:700}.scanbox{background:#e8f0f5;padding:12px;border-radius:10px;margin-top:10px}
.scaninput{flex:1 1 160px;min-width:150px;width:auto}
#cameraOverlay{display:none;position:fixed;inset:0;z-index:1000;background:#e9eef3;overflow:auto;padding:12px}
#repReader{max-width:650px;margin:16px auto}#cameraOverlay button{display:block;margin:15px auto}
</style></head><body><main>
<a id="returnToPicking" href="/fbs">← Вернуться к сборке</a><h1>📦 Пополнение</h1>
<div class="small">Авторизация по бейджу сотрудника. Начало и завершение задач фиксируются автоматически. Сборка FBS не меняется.</div>
<div id="who" class="small" style="margin-top:8px"></div>
<div class="card"><h2>Самостоятельное пополнение</h2>
<p class="small">Выбери ячейку и отсканируй товар в резерве. Задача начнётся автоматически. При размещении проверь ШК ячейки и укажи фактическое количество. МойСклад не изменяется.</p>
<div class="actions"><select id="selfLocation" style="flex:1 1 150px;min-width:135px" aria-label="Ячейка назначения"><option value="">Выбери ячейку</option></select>
<input id="selfBarcode" class="scaninput" placeholder="ШК товара" autocomplete="off" autocorrect="off" autocapitalize="none">
<button type="button" id="selfCreate">Начать пополнение</button></div></div>
<div class="card"><h2>Мои задачи</h2><div id="mine">Загрузка…</div></div>
<div class="card"><h2>Свободные задачи <button id="refreshList" class="secondary" type="button" style="float:right">Обновить</button></h2><div id="available">Загрузка…</div></div>
<div id="msg" role="status"></div>
</main>
<div id="cameraOverlay"><button type="button" id="closeCamera" class="secondary">✕ Закрыть камеру</button>
<h2 style="text-align:center" id="cameraTitle">Сканируй</h2><div id="repReader"></div></div>
<script>
function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}
async function readApiJson(r){
 const body=await r.text();
 try{return JSON.parse(body)}catch(_e){throw Error('Сервер вернул не JSON. Обнови страницу и повтори действие.')}
}
let tasks=[],sending=false;
function block(t,mine){
 const note=t.note?'<p class="small">'+esc(t.note)+'</p>':'';
 const plan=t.planned_qty?' · требуется '+esc(t.planned_qty)+' шт.':'';
 let buttons='';
 if(mine){
  if(t.verify_required){
   const stage=!t.verified_product_at?'product':!t.verified_location_at?'location':'completed';
   if(stage==='completed'){
    buttons='<div class="ok">✓ Товар и место назначения подтверждены</div>'+
    '<div class="actions"><input type="number" id="qty-'+esc(t.id)+'" placeholder="Шт." min="1" step="1" inputmode="numeric">'+
    '<button data-id="'+esc(t.id)+'" data-action="complete">Завершить размещение</button>'+
    '<button class="secondary" data-id="'+esc(t.id)+'" data-action="block">Проблема</button></div>';
   }else{
    const title=stage==='product'?'Шаг 1. Отсканируй ШК товара':'Шаг 2. Отсканируй ШК ячейки '+esc(t.line);
    buttons='<div class="scanbox"><b>'+title+'</b><div class="small">'+
      (stage==='product'?'Сверим товар с артикулом из задания':'При постановке товара сверим код с адресом назначения')+
      '</div><div class="actions"><input class="scaninput" autocomplete="off" autocorrect="off" autocapitalize="none" data-scan-task="'+esc(t.id)+'" id="scan-'+esc(t.id)+'" placeholder="Штрихкод">'+
      '<button data-id="'+esc(t.id)+'" data-stage="'+stage+'" data-action="verify">Проверить ШК</button>'+
      '<button class="secondary" data-id="'+esc(t.id)+'" data-stage="'+stage+'" data-action="camera">📷 Камера</button>'+
      '<button class="secondary" data-id="'+esc(t.id)+'" data-action="block">Проблема</button></div></div>';
   }
  }else{
   buttons='<p class="small">Старая задача: двойная проверка не включена</p>'+
   '<div class="actions"><input type="number" id="qty-'+esc(t.id)+'" placeholder="Шт." min="1" step="1" inputmode="numeric">'+
   '<button data-id="'+esc(t.id)+'" data-action="complete">Завершить</button>'+
   '<button class="secondary" data-id="'+esc(t.id)+'" data-action="block">Проблема</button></div>';
  }
 }else buttons='<div class="actions"><button data-id="'+esc(t.id)+'" data-action="start">Принять задачу</button></div>';
 return '<div class="task"><b>'+esc(t.article)+'</b> · линия '+esc(t.line)+
 (t.priority==='urgent'?' <span class="urgent">СРОЧНО</span>':'')+
 '<div class="small">'+(t.product_name?esc(t.product_name)+' · ':'')+esc(t.status)+plan+'</div>'+note+buttons+'</div>';
}
// A hardware scanner types into the focused field. load() re-renders tasks,
// so always restore focus when a worker accepts a task or completes a scan.
function focusTaskInput(preferredId,force=false){
 const mine=tasks.filter(t=>t.status==='started');
 const task=mine.find(t=>t.id===preferredId)||mine[0];
 if(!task)return;
 const active=document.activeElement;
 if(!force&&active&&(active.id==='selfBarcode'||active.id==='selfLocation'))return;
 const verified=!task.verify_required||(task.verified_product_at&&task.verified_location_at);
 const target=document.getElementById((verified?'qty-':'scan-')+task.id);
 if(!target)return;
 requestAnimationFrame(function(){
  if(!target.isConnected||cameraActive)return;
  try{target.focus({preventScroll:true})}catch(_e){target.focus()}
 });
}
function retryScanFocus(taskId){
 const input=document.getElementById('scan-'+taskId);
 if(!input||cameraActive)return;
 input.value='';
 try{input.focus({preventScroll:true})}catch(_e){input.focus()}
}
async function load(focusTaskId=null,forceFocus=false){
 const [me,resp]=await Promise.all([fetch('/api/auth/me'),fetch('/api/replenishment/tasks',{cache:'no-store'})]);
 if(me.status===401||resp.status===401){location.href='/login';return}
 const a=await readApiJson(me),d=await readApiJson(resp);
 if(!resp.ok)throw Error(d.error||'Не удалось загрузить задачи');
 document.getElementById('who').textContent='Сотрудник: '+a.employee.name;
 tasks=d.tasks||[];
 const sel=document.getElementById('selfLocation'),chosen=sel.value;
 sel.innerHTML='<option value="">Выбери ячейку</option>'+(d.locations||[]).map(function(v){
  return '<option value="'+esc(v.label)+'">'+esc(v.label)+'</option>';
 }).join('');
 if((d.locations||[]).some(v=>v.label===chosen))sel.value=chosen;
 const mine=tasks.filter(function(t){return t.status==='started'});
 const available=tasks.filter(function(t){return t.status==='new'});
 document.getElementById('mine').innerHTML=mine.length?mine.map(function(t){return block(t,true)}).join(''):'<p class="small">Нет задач в работе.</p>';
 document.getElementById('available').innerHTML=available.length?available.map(function(t){return block(t,false)}).join(''):'<p class="small">Очередь пуста.</p>';
 focusTaskInput(focusTaskId,forceFocus);
}

async function createSelfTask(){
 if(sending)return;
 const barcode=document.getElementById('selfBarcode').value.trim();
 const line=document.getElementById('selfLocation').value;
 const msg=document.getElementById('msg'),btn=document.getElementById('selfCreate');
 if(!line||!barcode){msg.className='bad';msg.textContent='Выбери ячейку и отсканируй ШК товара';return;}
 sending=true;btn.disabled=true;
 try{
  const r=await fetch('/api/replenishment/self-create',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({barcode:barcode,line:line})});
  const d=await readApiJson(r);
  if(!r.ok)throw Error(d.error||'Не удалось начать пополнение');
  document.getElementById('selfBarcode').value='';
  msg.className='ok';msg.textContent='Задача начата: '+d.article+'. При размещении сканируй ШК ячейки.';
  await load(d.task_id,true);
 }catch(e){msg.className='bad';msg.textContent=e.message;}
 finally{sending=false;btn.disabled=false;}
}
document.getElementById('selfCreate').addEventListener('click',createSelfTask);
document.getElementById('selfBarcode').addEventListener('keydown',function(e){
 if(e.key==='Enter'){e.preventDefault();createSelfTask();}
});
let scanner=null,cameraActive=false;
async function stopRepCamera(){
 document.getElementById('cameraOverlay').style.display='none';
 if(scanner){const old=scanner;scanner=null;
  try{await old.stop()}catch(e){}
  try{old.clear()}catch(e){}
 }
 cameraActive=false;
}
document.getElementById('closeCamera').addEventListener('click',stopRepCamera);
async function openRepCamera(id,stage){
 if(cameraActive)return;
 if(typeof Html5Qrcode==='undefined'){document.getElementById('msg').textContent='Камера недоступна. Используй HID-сканер или поле ШК.';return}
 cameraActive=true;
 document.getElementById('cameraOverlay').style.display='block';
 document.getElementById('cameraTitle').textContent=stage==='product'?'ШК товара':'ШК ячейки';
 scanner=new Html5Qrcode('repReader');
 let captured=false;
 try{
  await scanner.start({facingMode:'environment'},{fps:10,qrbox:{width:270,height:220}},
    async function(code){
      if(captured)return;captured=true;
      await stopRepCamera();
      await verifyScan(id,stage,code);
    },()=>{});
 }catch(e){
  await stopRepCamera();
  document.getElementById('msg').textContent='Не получилось открыть камеру. Разреши доступ или используй сканер.';
 }
}
async function verifyScan(id,stage,barcode){
 if(sending)return;
 barcode=String(barcode||'').trim();
 if(!barcode){document.getElementById('msg').textContent='Сначала отсканируй ШК';retryScanFocus(id);return}
 sending=true;
 const msg=document.getElementById('msg');msg.className='';msg.textContent='Проверяю штрихкод…';
 try{
  const r=await fetch('/api/replenishment/verify',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({task_id:id,stage:stage,barcode:barcode})}),d=await readApiJson(r);
  if(!r.ok)throw Error(d.error||'ШК не прошёл проверку');
  msg.className='ok';msg.textContent=stage==='product'?'✓ Товар проверен. Теперь доставь его к ячейке и отсканируй её ШК.':'✓ Ячейка подтверждена. Укажи фактическое количество.';
  await load(id,true);
 }catch(e){msg.className='bad';msg.textContent=e.message;retryScanFocus(id)}
 finally{sending=false}
}
document.addEventListener('keydown',function(e){
 if(e.key!=='Enter')return;
 const input=e.target.closest('input[data-scan-task]');if(!input)return;
 e.preventDefault();
 const task=tasks.find(t=>t.id===input.dataset.scanTask);
 if(!task)return;
 verifyScan(task.id,!task.verified_product_at?'product':'location',input.value);
});

async function action(e){
 const btn=e.target.closest('button[data-action]');if(!btn||sending)return;
 const type=btn.dataset.action,id=btn.dataset.id;let quantity=null,note='';
 if(type==='camera'){await openRepCamera(id,btn.dataset.stage);return}
 if(type==='verify'){
  const input=document.getElementById('scan-'+id);
  await verifyScan(id,btn.dataset.stage,input&&input.value);return
 }
 if(type==='complete'){
  const input=document.getElementById('qty-'+id);quantity=Number(input&&input.value);
  if(!Number.isSafeInteger(quantity)||quantity<1){alert('Укажи фактически пополненное количество в штуках');return}
 }
 if(type==='block'){note=prompt('Что помешало пополнению?');if(note===null)return;if(note.trim().length<5){alert('Укажи причину хотя бы из 5 символов');return}}
 sending=true;btn.disabled=true;
 try{
  const r=await fetch('/api/replenishment/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:id,action:type,quantity:quantity,note:note})});
  const d=await readApiJson(r);if(!r.ok)throw Error(d.error||'Ошибка');
  document.getElementById('msg').textContent='Задача обновлена';
  await load(type==='start'?id:null,type==='start');
 }catch(e){document.getElementById('msg').textContent=e.message;btn.disabled=false}
 finally{sending=false}
}
document.getElementById('available').addEventListener('click',action);
document.getElementById('mine').addEventListener('click',action);
document.getElementById('refreshList').addEventListener('click',function(){if(!sending)load().catch(function(e){document.getElementById('msg').textContent=e.message})});
async function switchOperation(operation){
 const r=await fetch('/api/operation/enter',{
  method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({operation:operation})
 });
 const d=await readApiJson(r);
 if(!r.ok)throw Error(d.error||'Не удалось переключить учёт времени');
}
document.getElementById('returnToPicking').addEventListener('click',async function(e){
 e.preventDefault();
 if(sending)return;
 try{
  await switchOperation('picking');
  location.href='/fbs';
 }catch(err){
  const msg=document.getElementById('msg');
  msg.className='bad';
  msg.textContent='Учёт времени не переключён: '+err.message;
 }
});
switchOperation('replenishment').then(load).catch(function(e){
 const msg=document.getElementById('msg');
 msg.className='bad';
 msg.textContent='Не удалось включить учёт пополнения: '+e.message;
 document.getElementById('mine').textContent='Проверь соединение и обнови страницу.';
});
</script></body></html>"""


@replenishment_bp.get("/manager/replenishment")
def manager_replenishment_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return MANAGER_PAGE


@replenishment_bp.get("/replenishment")
def replenishment_worker_page():
    ensure_db()
    if not get_session_employee():
        return redirect("/login")
    return WORKER_PAGE


@replenishment_bp.get("/api/manager/replenishment/tasks")
def manager_replenishment_tasks():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    rows = query_all(TASK_SELECT + " ORDER BY CASE t.status WHEN 'new' THEN 0 WHEN 'started' THEN 1 ELSE 2 END, CASE t.priority WHEN 'urgent' THEN 0 ELSE 1 END, t.created_at DESC LIMIT 150")
    events = query_all("""
        SELECT ev.kind, ev.actor, ev.note, ev.quantity, ev.created_at,
               t.article, t.line_code
        FROM replenishment_task_events ev
        JOIN replenishment_tasks t ON t.id = ev.task_id
        ORDER BY ev.created_at DESC
        LIMIT 70
    """)
    return jsonify(tasks=[_item(r) for r in rows], events=[
        {"kind": r[0], "actor": r[1], "note": r[2], "quantity": r[3],
         "created_at": r[4], "article": r[5], "line": r[6]}
        for r in events
    ])


@replenishment_bp.post("/api/manager/replenishment/create")
def manager_replenishment_create():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    data = _json_required()
    if data is None:
        return jsonify(error="Ожидается JSON"), 400
    article = str(data.get("article") or "").strip().upper()
    line = str(data.get("line") or "").strip().upper()
    note = str(data.get("note") or "").strip()
    product_id = str(data.get("product_id") or "").strip()
    priority = str(data.get("priority") or "normal").strip()
    quantity = data.get("planned_qty")
    if not 1 <= len(article) <= 100 or not 1 <= len(line) <= 80 or len(note) > 500 or priority not in ("normal", "urgent"):
        return jsonify(error="Проверь артикул, ячейку, приоритет и примечание"), 400
    if quantity not in (None, ""):
        if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 10000000:
            return jsonify(error="Количество должно быть целым положительным числом"), 400
    else:
        quantity = None
    if not product_id:
        return jsonify(error="Выбери товар из каталога МойСклад"), 400
    from app import _get_product_list
    try:
        product = next((p for p in _get_product_list() if str(p.get("id")) == product_id), None)
    except Exception:
        return jsonify(error="МойСклад временно недоступен; непроверенная задача не создана"), 502
    if not product or str(product.get("article") or "").strip().casefold() != article.casefold():
        return jsonify(error="Товар и артикул не соответствуют МойСклад"), 409
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    actor = get_session_employee()
    admin_name = actor["name"] if actor and actor["role"] == "manager" else "Руководитель (ПК)"
    task_id = str(uuid.uuid4())
    created = now_iso()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if not DATABASE_URL:
                cur.execute("BEGIN IMMEDIATE")
            conflict = _check_destination(cur, line, product_id, article)
            if conflict:
                return jsonify(error=conflict), 409
            existing = _open_location_task(cur, line)
            if existing:
                return jsonify(
                    error="По ячейке уже есть открытое пополнение: "
                    + str(existing[1]) + ". Заверши или отмени его перед новым заданием."
                ), 409
            cur.execute(sql("""
                INSERT INTO replenishment_tasks
                (id, article, line_code, planned_qty, priority, note, status, created_at, created_by,
                 product_id, product_name, verify_required)
                VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, 1)
            """), (task_id, article, line, quantity, priority, note, created, admin_name,
                    product_id, str(product.get("name") or "")))
            _event(cur, task_id, actor["id"] if actor else None, admin_name, "created", note, quantity)
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True, task_id=task_id), 201


@replenishment_bp.post("/api/manager/replenishment/action")
def manager_replenishment_action():
    ensure_db()
    if not manager_access():
        return jsonify(error="Доступ руководителю"), 403
    data = _json_required()
    if data is None:
        return jsonify(error="Ожидается JSON"), 400
    task_id = str(data.get("task_id") or "").strip()
    action = str(data.get("action") or "").strip()
    note = str(data.get("note") or "").strip()
    if not task_id or action not in ("cancel", "reopen", "enable_checks") or not 5 <= len(note) <= 500:
        return jsonify(error="Укажи действие и причину от 5 до 500 символов"), 400
    employee = get_session_employee()
    actor = employee["name"] if employee and employee["role"] == "manager" else "Руководитель (ПК)"
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql("SELECT status, article, line_code FROM replenishment_tasks WHERE id = ?"), (task_id,))
            row = cur.fetchone()
            if not row:
                return jsonify(error="Задача не найдена"), 404
            if action == "cancel":
                from_status = ("new", "started", "blocked")
                new_status = "cancelled"
            elif action == "reopen":
                from_status = ("blocked",)
                new_status = "new"
                existing = _open_location_task(cur, row[2], exclude_id=task_id)
                if existing:
                    return jsonify(error="По ячейке уже есть другая открытая задача: "+str(existing[1])), 409
                cur.execute(sql("SELECT product_id FROM replenishment_tasks WHERE id=?", (task_id,))
                product_id = cur.fetchone()[0]
                conflict = _check_destination(cur, row[2], product_id, row[1])
                if conflict:
                    return jsonify(error=conflict), 409
            else:
                from_status = ("new",)
                new_status = "new"
                cur.execute(sql("SELECT id FROM replenishment_locations WHERE label = ? AND active = 1"), (row[2],))
                if not cur.fetchone():
                    return jsonify(error="Зарегистрируй ячейку перед включением проверки"), 409
            if row[0] not in from_status:
                return jsonify(error="Задача уже изменила статус; обнови список"), 409
            # Conditional update protects against another worker modifying the task.
            placeholders = ",".join(["?"] * len(from_status))
            if action == "reopen":
                extra = ", assigned_employee_id = NULL, started_at = NULL, completed_at = NULL, completed_qty = NULL, verified_product_barcode = NULL, verified_product_at = NULL, verified_location_barcode = NULL, verified_location_at = NULL"
            elif action == "enable_checks":
                extra = ", verify_required = 1"
            else:
                extra = ""
            condition = " AND verify_required = 0" if action == "enable_checks" else ""
            cur.execute(sql("UPDATE replenishment_tasks SET status = ?" + extra + " WHERE id = ? AND status IN (" + placeholders + ")" + condition),
                        (new_status, task_id) + from_status)
            if cur.rowcount != 1:
                return jsonify(error="Статус задачи изменился; обнови список"), 409
            _event(cur, task_id, employee["id"] if employee else None, actor, action, note)
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True)


@replenishment_bp.get("/api/replenishment/tasks")
def worker_replenishment_tasks():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Войди по бейджу"), 401
    rows = query_all(TASK_SELECT +
        """ WHERE t.status = 'new' OR (t.status = 'started' AND t.assigned_employee_id = ?)
            ORDER BY CASE t.status WHEN 'started' THEN 0 ELSE 1 END,
                     CASE t.priority WHEN 'urgent' THEN 0 ELSE 1 END, t.created_at
            LIMIT 100 """, (emp["id"],))
    locations = query_all("""
        SELECT label FROM replenishment_locations WHERE active = 1 ORDER BY label LIMIT 1000
    """)
    return jsonify(tasks=[_item(r) for r in rows],
                   locations=[{"label": row[0]} for row in locations])


@replenishment_bp.post("/api/replenishment/self-create")
def worker_replenishment_self_create():
    """Start a worker's own replenishment with a verified product scan; no inventory write."""
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Войди по бейджу"), 401
    data = _json_required()
    if data is None:
        return jsonify(error="Ожидается JSON"), 400
    from replenishment_checks import _scan_code
    barcode = _scan_code(data.get("barcode"))
    line = str(data.get("line") or "").strip().upper()
    if not 3 <= len(barcode) <= 160 or not 1 <= len(line) <= 80:
        return jsonify(error="Сканируй ШК товара и выбери зарегистрированную ячейку"), 400
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    if not query_one("SELECT id FROM replenishment_locations WHERE label = ? AND active = 1", (line,)):
        return jsonify(error="Ячейка не зарегистрирована руководителем"), 409
    try:
        from app import find_product_by_any_barcode
        product, _pack = find_product_by_any_barcode(barcode)
    except Exception:
        return jsonify(error="Каталог товаров недоступен. Не создавай задачу наугад."), 502
    article = str((product or {}).get("article") or "").strip().upper()
    product_id = str((product or {}).get("id") or "").strip()
    if not product_id or not article:
        return jsonify(error="Товар по ШК не найден в каталоге МойСклад"), 404
    task_id = str(uuid.uuid4())
    created = now_iso()
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                if not DATABASE_URL:
                    cur.execute("BEGIN IMMEDIATE")
                conflict = _check_destination(cur, line, product_id, article)
                if conflict:
                    return jsonify(error=conflict), 409
                existing = _open_location_task(cur, line)
                if existing:
                    return jsonify(
                        error="По ячейке уже есть открытая задача: " + str(existing[1])
                        + ". Заверши или отмени её перед новым пополнением."
                    ), 409
                cur.execute(sql("""
                    INSERT INTO replenishment_tasks
                    (id, article, line_code, planned_qty, priority, note, status,
                     created_at, started_at, created_by, assigned_employee_id,
                     product_id, product_name, verify_required,
                     verified_product_barcode, verified_product_at)
                    VALUES (?, ?, ?, NULL, 'normal', '', 'started',
                            ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """), (task_id, article, line, created, created, emp["name"], emp["id"],
                        product_id, str(product.get("name") or ""), barcode, created))
                _event(cur, task_id, emp["id"], emp["name"], "created", "Создано сборщиком")
                _event(cur, task_id, emp["id"], emp["name"], "start", "Взят товар из резерва")
                _event(cur, task_id, emp["id"], emp["name"], "product_verified", barcode)
                conn.commit()
            finally:
                cur.close()
    except Exception:
        if query_one("""SELECT id FROM replenishment_tasks
                       WHERE article = ? AND line_code = ? AND status IN ('new','started') LIMIT 1""",
                     (article, line)):
            return jsonify(error="Такая задача уже создана. Обнови список."), 409
        return jsonify(error="Не удалось сохранить задачу. Проверь связь и обнови страницу."), 503
    return jsonify(ok=True, task_id=task_id, article=article, line=line), 201


@replenishment_bp.post("/api/replenishment/action")
def worker_replenishment_action():
    ensure_db()
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Войди по бейджу"), 401
    data = _json_required()
    if data is None:
        return jsonify(error="Ожидается JSON"), 400
    task_id = str(data.get("task_id") or "").strip()
    action = str(data.get("action") or "").strip()
    note = str(data.get("note") or "").strip()
    qty = data.get("quantity")
    if not task_id or action not in ("start", "complete", "block"):
        return jsonify(error="Некорректное действие"), 400
    if action == "complete" and (isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 10000000):
        return jsonify(error="Фактическое количество должно быть целым положительным числом"), 400
    if action == "block" and not 5 <= len(note) <= 500:
        return jsonify(error="Укажи причину проблемы от 5 до 500 символов"), 400
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            if action in ("start", "complete"):
                cur.execute(sql("""
                    SELECT article, line_code, product_id FROM replenishment_tasks
                    WHERE id = ?
                """), (task_id,))
                current = cur.fetchone()
                if not current:
                    return jsonify(error="Задача не найдена"), 404
                article, line, product_id = current
                conflict = _check_destination(cur, line, product_id, article)
                if conflict:
                    return jsonify(error=conflict), 409
            if action == "start":
                existing = _open_location_task(cur, line, exclude_id=task_id)
                if existing and existing[0] != task_id:
                    # An existing queued item may be valid legacy data. Do not
                    # take a second task into work while another is started.
                    cur.execute(sql("""
                        SELECT 1 FROM replenishment_tasks
                        WHERE line_code=? AND status='started' AND id<>? LIMIT 1
                    """), (line, task_id))
                    if cur.fetchone():
                        return jsonify(error="Ячейка уже пополняется другим заданием"), 409
                cur.execute(sql("""
                    UPDATE replenishment_tasks
                    SET status = 'started', assigned_employee_id = ?, started_at = ?
                    WHERE id = ? AND status = 'new'
                """), (emp["id"], now_iso(), task_id))
            elif action == "complete":
                cur.execute(sql("""
                    UPDATE replenishment_tasks
                    SET status = 'completed', completed_at = ?, completed_qty = ?
                    WHERE id = ? AND status = 'started' AND assigned_employee_id = ?
                      AND (verify_required = 0 OR
                           (verified_product_at IS NOT NULL AND verified_location_at IS NOT NULL))
                """), (now_iso(), qty, task_id, emp["id"]))
            else:
                cur.execute(sql("""
                    UPDATE replenishment_tasks
                    SET status = 'blocked', completed_at = ?
                    WHERE id = ? AND status = 'started' AND assigned_employee_id = ?
                """), (now_iso(), task_id, emp["id"]))
            if cur.rowcount != 1:
                return jsonify(error="Задача недоступна или уже изменена другим сотрудником"), 409
            _event(cur, task_id, emp["id"], emp["name"], action, note, qty if action == "complete" else None)
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True)
