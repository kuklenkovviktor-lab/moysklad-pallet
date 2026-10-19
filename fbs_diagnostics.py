"""ONIX WMS: isolated first-barcode diagnostics, no FBS order writes."""
import statistics
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from flask import Blueprint, jsonify, redirect, request
from wms import db_connect, get_session_employee, manager_access, sql

diag_bp = Blueprint("fbs_diagnostics", __name__)
_INIT_LOCK = threading.Lock()
_DB_INITIALIZED = False
ZONES = ["Возле роутера"] + ["Линия " + c for c in "АБВГДЕЖЗИКЛМН"] + ["Другая зона"]
NETWORKS = ["Wi-Fi 2,4 ГГц", "Wi-Fi 5 ГГц", "Мобильная точка доступа", "Другая сеть"]


def ensure_diagnostics_db():
    global _DB_INITIALIZED
    if _DB_INITIALIZED:
        return
    with _INIT_LOCK:
        if _DB_INITIALIZED:
            return
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql("""
                    CREATE TABLE IF NOT EXISTS fbs_scan_diagnostics (
                        id TEXT PRIMARY KEY,
                        employee_id TEXT NOT NULL,
                        zone TEXT NOT NULL,
                        network TEXT NOT NULL,
                        total_ms REAL NOT NULL,
                        server_ms REAL NOT NULL,
                        http_status INTEGER NOT NULL,
                        ok INTEGER NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """))
                cur.execute(sql("""
                    CREATE INDEX IF NOT EXISTS idx_fbs_scan_diag_created
                    ON fbs_scan_diagnostics(created_at)
                """))
                conn.commit()
                _DB_INITIALIZED = True
            finally:
                cur.close()


def _clamped_float(value):
    if isinstance(value, bool):
        raise ValueError("Некорректное измерение")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Некорректное измерение")
    if not 0 <= number <= 120000:
        raise ValueError("Время должно быть от 0 до 120000 мс")
    return round(number, 1)


@diag_bp.get("/fbs-diagnostics")
def diagnostics_page():
    if not get_session_employee():
        return redirect("/login")
    return DIAGNOSTICS_PAGE


@diag_bp.post("/api/fbs/diagnostics")
def record_diagnostic():
    emp = get_session_employee()
    if not emp:
        return jsonify(error="Требуется вход по бейджу"), 401
    data = request.get_json(silent=True) or {}
    try:
        request_id = str(uuid.UUID(str(data.get("id", ""))))
        zone = str(data.get("zone", ""))
        network = str(data.get("network", ""))
        if zone not in ZONES or network not in NETWORKS:
            return jsonify(error="Выбери участок и тип сети"), 400
        total = _clamped_float(data.get("total_ms"))
        server = _clamped_float(data.get("server_ms"))
        http_status = int(data.get("http_status"))
        if not 0 <= http_status <= 599 or server > total + 100:
            return jsonify(error="Недостоверные данные измерения"), 400
        if not isinstance(data.get("ok"), bool):
            return jsonify(error="Некорректный результат"), 400
    except (ValueError, TypeError, OverflowError):
        return jsonify(error="Некорректные данные измерения"), 400

    ensure_diagnostics_db()
    # Idempotent writes: a retry after bad Wi-Fi will not double-count.
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql("""
                INSERT INTO fbs_scan_diagnostics
                (id, employee_id, zone, network, total_ms, server_ms, http_status, ok, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO NOTHING
            """), (request_id, emp["id"], zone, network, total, server, http_status,
                   int(data["ok"]), datetime.now(timezone.utc).isoformat()))
            conn.commit()
        finally:
            cur.close()
    return jsonify(ok=True)


def _summary(measurements):
    successes = [x for x in measurements if x["ok"]]
    sorted_totals = sorted(x["total_ms"] for x in successes)
    n = len(sorted_totals)
    p95 = sorted_totals[max(0, (95 * n + 99) // 100 - 1)] if n else None
    return {
        "samples": len(measurements),
        "ok": n,
        "errors": len(measurements) - n,
        "avg_ms": round(statistics.mean(sorted_totals)) if n else None,
        "median_ms": round(statistics.median(sorted_totals)) if n else None,
        "p95_ms": round(p95) if n else None,
        "max_ms": round(max(sorted_totals)) if n else None,
        "avg_server_ms": round(statistics.mean(x["server_ms"] for x in successes)) if n else None,
        "avg_other_ms": round(statistics.mean(max(0, x["total_ms"] - x["server_ms"]) for x in successes)) if n else None,
    }


@diag_bp.get("/api/manager/fbs-diagnostics")
def manager_diagnostic_data():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_diagnostics_db()
    # Local warehouse calendar day, not UTC calendar day.
    now = datetime.now(ZoneInfo("Europe/Moscow"))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = start.date().isoformat()
    start_utc = start.astimezone(timezone.utc).isoformat()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql("""
                SELECT d.employee_id, COALESCE(e.name, d.employee_id), d.zone, d.network,
                       d.total_ms, d.server_ms, d.http_status, d.ok, d.created_at
                FROM fbs_scan_diagnostics d LEFT JOIN employees e ON e.id = d.employee_id
                WHERE d.created_at >= ?
                ORDER BY d.created_at DESC LIMIT 3000
            """), (start_utc,))
            raw = cur.fetchall()
        finally:
            cur.close()
    rows = [
        {"employee_id": r[0], "employee": r[1], "zone": r[2], "network": r[3],
         "total_ms": float(r[4]), "server_ms": float(r[5]), "http_status": int(r[6]),
         "ok": bool(r[7]), "created_at": r[8]}
        for r in raw
    ]
    grouped = defaultdict(list)
    for item in rows:
        grouped[(item["zone"], item["network"])].append(item)
    summary = [
        {"zone": zone, "network": network, **_summary(items)}
        for (zone, network), items in grouped.items()
    ]
    summary.sort(key=lambda x: (ZONES.index(x["zone"]) if x["zone"] in ZONES else 99,
                                 NETWORKS.index(x["network"]) if x["network"] in NETWORKS else 99))
    return jsonify(date=day, total=len(rows), groups=summary, recent=rows[:40])


@diag_bp.get("/manager/fbs-diagnostics")
def manager_diagnostic_page():
    if not manager_access():
        return redirect("/manager-login")
    return MANAGER_DIAGNOSTICS_PAGE


DIAGNOSTICS_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ONIX · Диагностика сканов</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e9eef3;color:#263744;font-family:system-ui,-apple-system,sans-serif}
main{max-width:860px;margin:auto;padding:14px}.card{padding:16px;border-radius:14px;background:white;margin:12px 0;border:1px solid #d3dfe7}
h1{font-size:23px;margin:12px 0}h2{font-size:18px;margin:0 0 12px}p{line-height:1.4}
a{color:#325d78}select,input,button{padding:12px;font:inherit;border:1px solid #b9cbd7;border-radius:9px;width:100%;background:white}
button{background:#47677b;color:#fff;font-weight:700;cursor:pointer}
label{font-size:14px;font-weight:650;display:block;margin:10px 0 6px}
#code{font-size:20px;font-weight:bold;text-align:center;height:56px}
.status{padding:14px;background:#edf4f7;border-radius:9px;text-align:center;font-size:22px;font-weight:800}
.metric{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.metric>div{background:#f1f5f8;border-radius:9px;padding:11px;text-align:center}
.metric b{font-size:23px;display:block}.metric small{font-size:11px;color:#5a6d7b}
.small{font-size:12px;color:#627584}.good{color:#27734c}.bad{color:#ae4938}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:520px){.row{grid-template-columns:1fr}.metric b{font-size:18px}}
</style></head><body><main>
<a href="/fbs">← Сборка FBS</a><h1>📶 Проверка скорости сканирования</h1>
<div class="card">
<p><b>Это тест, не сборка.</b> Сканируй один и тот же ШК товара 20 раз. ШК WB не нужен. Заказы и выработка не меняются.</p>
<div class="row">
<div><label for="zone">Где стоим</label>
<select id="zone">
<option>Возле роутера</option>
<option>Линия А</option><option>Линия Б</option><option>Линия В</option><option>Линия Г</option>
<option>Линия Д</option><option>Линия Е</option><option>Линия Ж</option><option>Линия З</option>
<option>Линия И</option><option>Линия К</option><option>Линия Л</option><option>Линия М</option>
<option>Линия Н</option><option>Другая зона</option>
</select></div>
<div><label for="network">Подключение</label>
<select id="network">
<option>Wi-Fi 2,4 ГГц</option><option>Wi-Fi 5 ГГц</option>
<option>Мобильная точка доступа</option><option>Другая сеть</option>
</select></div></div>
<label for="code">Сканируй ШК товара</label>
<input id="code" autocomplete="off" autocapitalize="off" spellcheck="false" inputmode="none" placeholder="Ожидаю сканер">
<div style="margin-top:9px"><button id="manual" type="button">Проверить введённый ШК</button></div>
<div class="status" id="stage" style="margin-top:12px">ГОТОВ К СКАНИРОВАНИЮ</div>
<p class="small" id="detail">Проведи 20 сканирований в каждой точке. Пока идёт поиск, подожди результата.</p>
</div>
<div class="card">
<h2>📊 Замеры в этой точке: <span id="count">0</span> / 20</h2>
<div class="metric">
 <div><b id="full">—</b><small>Последний отклик</small></div>
 <div><b id="server">—</b><small>Сервер ONIX</small></div>
 <div><b id="other">—</b><small>Сеть + браузер</small></div>
</div>
<p id="summary">Среднее: — · Максимум: — · Ошибок: 0</p>
<p class="small">Остаточная задержка включает Wi-Fi, интернет, передачу до Render и обработку браузером. Это не чистый ping.</p>
<button type="button" id="reset">Начать новую серию из 20</button>
<p class="small" id="sync">Результаты сохраняются в менеджерской панели.</p>
</div>
</main><script>
const input=document.getElementById('code'),zoneEl=document.getElementById('zone'),networkEl=document.getElementById('network');
let busy=false,series=[],buffer='',lastKey=0,clearTimer=null;
let queue=[];try{queue=JSON.parse(localStorage.getItem('onix_diag_queue')||'[]');if(!Array.isArray(queue))queue=[]}catch(e){queue=[]}
function persist(){try{localStorage.setItem('onix_diag_queue',JSON.stringify(queue.slice(-150)))}catch(e){}}
let flushing=false;
async function flush(){
 if(flushing||!queue.length)return;
 flushing=true;
 try{
  while(queue.length){
   const r=await fetch('/api/fbs/diagnostics',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(queue[0])});
   if(!r.ok){const d=await r.json().catch(()=>({}));document.getElementById('sync').textContent='Не удалось сохранить замер: '+(d.error||r.status);break}
   queue.shift();persist();
  }
 }catch(e){document.getElementById('sync').textContent='Связь прервана. Замеры в очереди и сохранятся при восстановлении интернета.'}
 finally{flushing=false;if(!queue.length)document.getElementById('sync').textContent='Все замеры записаны в менеджерскую панель.'}
}
function setStage(text,error){const s=document.getElementById('stage');s.textContent=text;s.className='status '+(error?'bad':'good')}
function focus(){if(!document.hidden)input.focus()}
function render(){
 const z=zoneEl.value,n=networkEl.value;
 const list=series.filter(x=>x.zone===z&&x.network===n);
 const valid=list.filter(x=>x.ok).map(x=>x.total_ms);
 document.getElementById('count').textContent=list.length;
 document.getElementById('summary').textContent='Среднее: '+(valid.length?Math.round(valid.reduce((a,b)=>a+b,0)/valid.length)+' мс':'—')+
  ' · Максимум: '+(valid.length?Math.round(Math.max(...valid))+' мс':'—')+
  ' · Ошибок: '+(list.length-valid.length);
}
function record(s){
 series.push(s);render();
 queue.push(s);persist();flush();
 if(series.filter(x=>x.zone===s.zone&&x.network===s.network).length===20){
  document.getElementById('detail').textContent='20 замеров готовы. Выбери следующую линию либо другую сеть.';
 }
}
async function scan(code){
 code=String(code||'').trim();
 if(!code||busy)return;
 busy=true;
 input.value='';setStage('ПОИСК ТОВАРА…',false);
 const started=performance.now(),z=zoneEl.value,n=networkEl.value;
 let total=0,server=0,status=0,ok=false,detail='';
 try{
  const r=await fetch('/api/fbs/product-info',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({barcode:code}),cache:'no-store'
  });
  status=r.status;
  server=Number(r.headers.get('X-ONIX-Server-Ms')||0);
  const d=await r.json();
  ok=r.ok;
  detail=ok?(d.article||d.name||'Товар найден'):(d.error||'Ошибка поиска');
  setStage(ok?'✅ ТОВАР НАЙДЕН':'❌ ОШИБКА СКАНА',!ok);
 }catch(e){
  detail='Нет ответа сервера: '+e.message;
  setStage('❌ НЕТ ОТВЕТА',true);
 }
 // Time until the result is updated in the terminal, not the second logging request.
 total=Math.max(0,Math.round(performance.now()-started));
 server=Math.max(0,Math.min(total,Math.round(server)));
 document.getElementById('full').textContent=total+' мс';
 document.getElementById('server').textContent=server?server+' мс':'—';
 document.getElementById('other').textContent=(total-server)+' мс';
 document.getElementById('detail').textContent=detail;
 record({id:crypto.randomUUID(),zone:z,network:n,total_ms:total,server_ms:server,http_status:status,ok});
 busy=false;focus();
}
document.getElementById('manual').onclick=()=>scan(input.value);
input.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key==='Tab'){e.preventDefault();scan(input.value)}});
document.addEventListener('keydown',e=>{
 if(document.activeElement===input||e.ctrlKey||e.altKey||e.metaKey)return;
 if(e.target&&['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;
 if(e.key==='Enter'||e.key==='Tab'){
  if(buffer.length>=4){e.preventDefault();const c=buffer;buffer='';scan(c)}
  else buffer='';
  return;
 }
 if(e.key.length!==1)return;
 if(performance.now()-lastKey>350)buffer='';
 lastKey=performance.now();buffer+=e.key;
 clearTimeout(clearTimer);clearTimer=setTimeout(()=>buffer='',900);
});
zoneEl.onchange=render;networkEl.onchange=render;
document.getElementById('reset').onclick=()=>{
 const z=zoneEl.value,n=networkEl.value;
 series=series.filter(x=>x.zone!==z||x.network!==n);
 render();document.getElementById('detail').textContent='Новая серия. Старые результаты сохранены в менеджерской панели.';focus();
};
window.addEventListener('pageshow',focus);
document.addEventListener('visibilitychange',focus);
setInterval(flush,10000);render();flush();focus();
</script></body></html>"""


MANAGER_DIAGNOSTICS_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>ONIX · Замеры сканирования</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e9eef3;color:#253744;font-family:system-ui,-apple-system,sans-serif}
main{max-width:1220px;margin:auto;padding:18px}h1{font-size:25px}h2{font-size:18px}
a{color:#365f7a}.card{background:#fff;border:1px solid #d4e0e6;border-radius:13px;padding:16px;margin:12px 0}
table{width:100%;border-collapse:collapse;text-align:left;font-size:13px}
th,td{border-bottom:1px solid #e1e8ed;padding:9px;white-space:nowrap}
.table-wrap{overflow:auto}th{background:#f1f5f8}.small{color:#617481;font-size:13px}
.mono{font-variant-numeric:tabular-nums}button{border:0;border-radius:8px;background:#47677b;color:#fff;padding:9px 15px;cursor:pointer}
</style></head><body><main>
<a href="/manager">← Панель управления</a>
<h1>📶 Диагностика скорости сканирования</h1>
<p class="small">Сегодня, по времени склада (Москва). Автообновление каждые 10 секунд. Тестовый скан не добавляет заказы WB.</p>
<div class="card"><h2>Сводка по участкам и типу сети</h2>
<div class="table-wrap"><table><thead><tr>
<th>Участок</th><th>Сеть</th><th>Замеров</th><th>Среднее</th><th>Медиана</th><th>P95</th><th>Макс.</th><th>Сервер ср.</th><th>Остальное ср.</th><th>Ошибок</th>
</tr></thead><tbody id="groups"><tr><td colspan="10">Ожидаю замеры…</td></tr></tbody></table></div></div>
<div class="card"><h2>Последние сканирования</h2>
<div class="table-wrap"><table><thead><tr><th>Время</th><th>Сотрудник</th><th>Участок</th><th>Сеть</th><th>Всего</th><th>Сервер</th><th>Остальное</th><th>Результат</th></tr></thead><tbody id="recent"></tbody></table></div></div>
<p class="small">«Остальное» = полный отклик минус серверная обработка: Wi-Fi + провайдер + путь до Render + браузер. Это не точный ping Wi-Fi.</p>
<p id="error" class="small"></p>
</main><script>
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function ms(v){return v==null?'—':Math.round(v)+' мс'}
function fmt(v){return new Date(v).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'})}
let loading=false;
async function load(){
 if(loading||document.hidden)return;loading=true;
 try{
  const r=await fetch('/api/manager/fbs-diagnostics',{cache:'no-store'});
  if(r.status===403){location.href='/manager-login';return}
  if(!r.ok)throw Error('Ошибка загрузки '+r.status);
  const d=await r.json();
  document.getElementById('groups').innerHTML=d.groups.length?d.groups.map(x=>
   '<tr><td>'+esc(x.zone)+'</td><td>'+esc(x.network)+'</td><td>'+x.samples+' ('+x.ok+' усп.)</td>'+
   '<td class="mono">'+ms(x.avg_ms)+'</td><td>'+ms(x.median_ms)+'</td><td>'+ms(x.p95_ms)+'</td>'+
   '<td>'+ms(x.max_ms)+'</td><td>'+ms(x.avg_server_ms)+'</td><td>'+ms(x.avg_other_ms)+'</td><td>'+x.errors+'</td></tr>'
  ).join(''):'<tr><td colspan="10">Сегодня ещё нет замеров</td></tr>';
  document.getElementById('recent').innerHTML=d.recent.map(x=>
   '<tr><td>'+fmt(x.created_at)+'</td><td>'+esc(x.employee)+'</td><td>'+esc(x.zone)+'</td>'+
   '<td>'+esc(x.network)+'</td><td>'+ms(x.total_ms)+'</td><td>'+ms(x.server_ms)+'</td>'+
   '<td>'+ms(Math.max(0,x.total_ms-x.server_ms))+'</td><td>'+(x.ok?'✅':'❌ '+x.http_status)+'</td></tr>'
  ).join('');
  document.getElementById('error').textContent='Всего замеров сегодня: '+d.total;
 }catch(e){document.getElementById('error').textContent=e.message}
 finally{loading=false}
}
load();setInterval(load,10000);document.addEventListener('visibilitychange',()=>{if(!document.hidden)load()});
</script></body></html>"""
