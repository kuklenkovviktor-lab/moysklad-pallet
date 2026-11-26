"""Receiving monitor presentation, without server or business dependencies."""

RECEIVING_MONITOR_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · Приёмка</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.app{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100dvh}.sidebar{background:#f7f9fb;border-right:1px solid #d7e0e7;padding:18px 12px;position:sticky;top:0;height:100dvh}.brand{padding:4px 8px 16px;border-bottom:1px solid #e0e7ec;margin-bottom:12px}.brand b{display:block;font-size:18px}.brand span{font-size:11px;color:#6b7785}.side-nav{display:grid;gap:7px}.side-nav a{display:flex;align-items:center;gap:9px;min-height:44px;padding:10px 12px;border-radius:10px;text-decoration:none;font-weight:750;font-size:13px;background:#eef3f6;color:#314657;border:1px solid transparent}.side-nav a:hover{background:#e3ebf0}.side-nav a.active{background:#48647a;color:#fff}.side-nav a.logout{margin-top:12px;background:#f3ecec;color:#794545}
.content{min-width:0;padding:16px}.wrap{max-width:1500px;margin:auto}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;box-shadow:0 2px 10px rgba(52,72,91,.07);margin-bottom:12px}
h1{margin:0;font-size:28px}.small{font-size:12px;color:#6b7785}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:12px 0}.kpi{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:15px}.kpi .v{font-size:30px;font-weight:850;margin-top:4px}.kpi .l{font-size:12px;color:#6b7785}
.table-wrap{overflow:auto;max-height:68vh;border:1px solid #dde5eb;border-radius:11px}table{width:100%;border-collapse:collapse;min-width:1050px;background:#fff}th,td{padding:10px 11px;border-bottom:1px solid #e4eaee;text-align:left;font-size:13px;white-space:nowrap}th{position:sticky;top:0;background:#eef3f6;z-index:2;color:#51606e}.article{font-weight:800}.muted{color:#71808d}.qty{font-weight:850}
.period-filters{display:flex;align-items:flex-end;gap:9px;flex-wrap:wrap;margin-top:14px}.period-filters label{display:grid;gap:4px;font-size:12px;font-weight:700;color:#51606e}.period-filters input{min-height:39px;border:1px solid #cbd9e2;border-radius:9px;background:#fff;padding:7px 10px;color:#25313d;font:inherit}.period-filters button{min-height:39px;border:1px solid #cbd9e2;border-radius:9px;background:#48647a;color:#fff;padding:8px 13px;font-weight:750;cursor:pointer}.period-filters button.secondary{background:#e8eef3;color:#314657}.period-message{margin-top:9px}.data-warning{margin-top:7px;color:#8a5c2e}
@media(max-width:1000px){.app{grid-template-columns:1fr}.sidebar{position:static;height:auto;border-right:0;border-bottom:1px solid #d7e0e7}.side-nav{grid-template-columns:repeat(2,minmax(0,1fr))}.side-nav a.logout{margin-top:0}.content{padding:12px}.kpis{grid-template-columns:1fr 1fr}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body>
<div class="app">
<aside class="sidebar">
 <div class="brand"><b><img src="/grit-logo.png?v=2" width="24" height="24" alt="" style="vertical-align:middle;border-radius:5px;margin-right:6px">GRIT WMS</b><span>Панель управления</span></div>
 <nav class="side-nav">
  <a href="/manager">🖥️ Главная</a>
  <a href="/fbs-journal">📋 Журнал FBS</a>
  <a href="/history">🔎 История ШК</a>
  <a class="active" href="/receiving-monitor">📥 Приёмка</a>
  <a href="/manager/integrations">🔌 Интеграции</a>
  <a href="/badges">🪪 Бейджи</a>
  <a href="/manager#qr" onclick="sessionStorage.setItem('openExitQr','1')">🔐 QR выхода</a>
  <a class="logout" href="#" onclick="managerLogout();return false">🚪 Выйти из ПК</a>
 </nav>
</aside>
<main class="content"><div class="wrap">
 <div class="card"><h1>📥 Приёмка</h1><div class="small">История операций приёмки. Обновление каждые 10 секунд.</div>
  <div class="period-filters">
   <label>С даты<input id="dateFrom" type="date" aria-label="Начало периода"></label>
   <label>По дату<input id="dateTo" type="date" aria-label="Конец периода"></label>
   <button id="showPeriod" type="button">Показать</button>
   <button id="showToday" class="secondary" type="button">Сегодня</button>
   <button id="showYesterday" class="secondary" type="button">Вчера</button>
  </div>
  <div id="periodMessage" class="small period-message"></div>
  <div class="small data-warning">Пока показатели рассчитаны по событиям GRIT: незавершённые документы МойСклад могут попадать в итог. Сверка будет исправлена отдельно.</div>
 </div>
 <div class="kpis">
  <div class="kpi"><div class="l">Единиц по операциям за период</div><div class="v" id="units">0</div></div>
  <div class="kpi"><div class="l">Операций приёмки</div><div class="v" id="operations">0</div></div>
  <div class="kpi"><div class="l">Приёмщиков за период</div><div class="v" id="workers">0</div></div>
  <div class="kpi"><div class="l">Событий завершения документов</div><div class="v" id="completed">0</div></div>
 </div>
 <div class="card">
  <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:9px"><b>Последние операции приёмки</b><span class="small">До 200 последних строк за выбранный период</span><span class="small">палетный ШК / ОСЗ / неполная палета</span></div>
  <div class="table-wrap"><table><thead><tr><th>Дата и время</th><th>Сотрудник</th><th>Артикул</th><th>Товар</th><th>ШК / SSCC</th><th>Количество</th><th>Документ</th><th>Тип</th></tr></thead><tbody id="rows"></tbody></table></div>
 </div>
</div></main></div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleString('ru-RU',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'})}catch(e){return ts}}
function dateKey(d){return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
function periodBounds(start,end){const s=new Date(start+'T00:00:00'),e=new Date(end+'T00:00:00');e.setDate(e.getDate()+1);return {from:s.toISOString(),to:e.toISOString()}}
let selectedBounds=null;
function applyPeriod(refresh=true){
 const start=document.getElementById('dateFrom').value,end=document.getElementById('dateTo').value;
 const note=document.getElementById('periodMessage');
 if(!start||!end||start>end){note.textContent='Укажите корректный период: дата начала не должна быть позже даты окончания.';return}
 selectedBounds=periodBounds(start,end);
 note.textContent='Период: '+start.split('-').reverse().join('.')+' — '+end.split('-').reverse().join('.');
 if(refresh)load().catch(()=>{note.textContent+=' · Не удалось получить данные, попробуйте обновить страницу.'});
}
function pickDay(offset,refresh=true){
 const d=new Date();d.setDate(d.getDate()+offset);
 const value=dateKey(d);
 document.getElementById('dateFrom').value=value;
 document.getElementById('dateTo').value=value;
 applyPeriod(refresh);
}
function typeLabel(t){return t==='external_pallet'?'ОСЗ / SSCC':t==='scan'?'Палета / товар':t==='complete'?'Завершение':'Операция'}
async function load(){
 const p=new URLSearchParams(selectedBounds),r=await fetch('/api/receiving/overview?'+p.toString());
 if(!r.ok&&r.status!==403)throw new Error('Ошибка загрузки истории приёмки: '+r.status);
 if(r.status===403){location.href='/manager-login';return}
 const d=await r.json();
 document.getElementById('units').textContent=d.units||0;
 document.getElementById('operations').textContent=d.operations||0;
 document.getElementById('workers').textContent=d.workers||0;
 document.getElementById('completed').textContent=d.completed||0;
 document.getElementById('rows').innerHTML=d.recent.length?d.recent.map(x=>'<tr><td>'+esc(fmt(x.created_at))+'</td><td><b>'+esc(x.employee)+'</b></td><td class="article">'+esc(x.article||'—')+'</td><td>'+esc(x.product_name||'—')+'</td><td class="muted">'+esc(x.object_code||'—')+'</td><td class="qty">'+esc(x.quantity??'—')+'</td><td>'+esc(x.document_name||x.document_id||'—')+'</td><td>'+esc(typeLabel(x.event_type))+'</td></tr>').join(''):'<tr><td colspan="8">За выбранный период приёмок нет</td></tr>';
}
async function managerLogout(){await fetch('/api/manager/logout',{method:'POST'});location.href='/manager-login'}
let monitorBusy=false;
async function monitorLoop(){
 if(!document.hidden&&!monitorBusy){
  monitorBusy=true;
  try{await load()}catch(e){}finally{monitorBusy=false}
 }
 setTimeout(monitorLoop,10000);
}
document.getElementById('showPeriod').addEventListener('click',()=>applyPeriod());
document.getElementById('showToday').addEventListener('click',()=>pickDay(0));
document.getElementById('showYesterday').addEventListener('click',()=>pickDay(-1));
pickDay(0,false);
monitorLoop();
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!monitorBusy)monitorLoop()});
</script></body></html>"""
