"""Manager-only shift history screen. Data are loaded from a read-only API."""

SHIFT_HISTORY_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#e9eef3"><title>ONIX WMS · История смен</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#e9eef3;color:#25313d;font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif}
main{max-width:1440px;margin:auto;padding:18px}
a{color:#48647a}.back{display:inline-block;margin-bottom:12px;text-decoration:none;font-weight:750}
h1{margin:0 0 5px;font-size:28px}h2{margin:0 0 12px;font-size:17px}
.small{font-size:12px;color:#647684}.note{font-size:13px;color:#536676;line-height:1.5}
.panel{padding:18px;background:#f8fafb;border:1px solid #d8e1e7;border-radius:15px;margin:14px 0}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:flex-end}
label{display:grid;gap:5px;color:#4c5d69;font-size:12px;font-weight:700}
input,select{height:41px;padding:8px;border:1px solid #c6d4dd;border-radius:9px;background:#fff;color:#25313d;font-size:14px}
button{height:41px;border:0;background:#48647a;color:white;padding:0 14px;border-radius:9px;font-weight:750;cursor:pointer}
button.secondary{background:#e4edf2;color:#314657}
button:disabled{opacity:.5;cursor:progress}
.presets{display:flex;gap:6px;flex-wrap:wrap;margin-top:12px}
.summary{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px}
.stat{background:#edf2f6;border-radius:11px;padding:11px;min-width:0}
.stat .value{font-size:23px;font-weight:850;margin-top:5px}
.status{font-size:13px;min-height:20px;margin-top:10px}.err{color:#a23d3d}
.table-wrap{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:930px;background:#fff}
th,td{border-bottom:1px solid #e0e8ed;padding:11px 9px;font-size:13px;text-align:left;vertical-align:top}
th{background:#eaf0f4;color:#536576;font-size:12px;white-space:nowrap}
td.num{font-variant-numeric:tabular-nums;font-weight:750}
tr.has-warning{background:#fffaf0}
.details-button{height:30px;padding:0 10px;font-size:12px}
.details-row>td{background:#f5f8fa;padding:13px 18px}
.details-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.inner{border-top:1px solid #dbe4ea;padding:8px 0;font-size:12px}
.inner:first-child{border-top:0}
.warning{color:#965a18;font-size:12px;font-weight:650}
.empty{text-align:center;color:#6b7785;padding:30px}
@media(max-width:920px){.summary{grid-template-columns:repeat(3,1fr)}.details-grid{grid-template-columns:1fr}}
@media(max-width:560px){main{padding:11px}.summary{grid-template-columns:repeat(2,1fr)}.toolbar{align-items:stretch}.toolbar>*{flex:1}.stat .value{font-size:20px}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=1"></head>
<body><main>
<a class="back" href="/manager">← Панель управления</a>
<h1>История смен и производительность</h1>
<p class="note">Подтверждённая сборка WB FBS, время операций и завершённые пополнения. Даты считаются по Москве. Данные берутся из истории ONIX, без изменения склада или МойСклада.</p>
<section class="panel">
  <div class="toolbar">
    <label>С даты <input id="dateFrom" type="date"></label>
    <label>По дату <input id="dateTo" type="date"></label>
    <label>Сотрудник <select id="employee"><option value="">Все сотрудники</option></select></label>
    <button type="button" id="show">Показать</button>
  </div>
  <div class="presets">
    <button type="button" class="secondary" data-days="1">Сегодня</button>
    <button type="button" class="secondary" data-days="0">Вчера</button>
    <button type="button" class="secondary" data-days="7">7 дней</button>
    <button type="button" class="secondary" data-days="31">31 день</button>
  </div>
  <div class="status" id="status" role="status"></div>
</section>
<section class="panel">
  <h2>Итог за выбранный период</h2>
  <div id="summary" class="summary"></div>
  <p class="small">Скорость = все подтверждённые заказы FBS / суммарное зафиксированное время сборки. «н/д» означает, что достоверно рассчитать скорость нельзя.</p>
</section>
<section class="panel">
  <h2>По дням и сотрудникам</h2>
  <div class="table-wrap">
    <table><thead><tr><th>Дата</th><th>Сотрудник</th><th>WB FBS</th><th>Заказов/час</th><th>Сборка</th><th>Пополнение</th><th>Заданий</th><th>Смены</th><th>Подробности</th></tr></thead>
    <tbody id="rows"><tr><td colspan="9" class="empty">Загружаю историю…</td></tr></tbody></table>
  </div>
  <p class="note">Пополнения считаются отдельно: количество по завершённым заданиям, время по записанным операциям. Если сотрудник не переключал режим, расхождение возможно.</p>
</section>
</main><script>
const fromEl=document.getElementById('dateFrom'),toEl=document.getElementById('dateTo');
const empEl=document.getElementById('employee'),statusEl=document.getElementById('status'),btn=document.getElementById('show');
let allEmployees=[],busy=false,renderedRows=[];
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmtN(n){return Number(n||0).toLocaleString('ru-RU')}
function duration(seconds){
 if(seconds==null)return 'н/д';
 const minutes=Math.round(Number(seconds)/60);
 return Math.floor(minutes/60)+' ч '+String(minutes%60).padStart(2,'0')+' мин';
}
function humanDate(ymd){return ymd.split('-').reverse().join('.')}
function humanStamp(iso){if(!iso)return '—';try{return new Date(iso).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})}catch(e){return iso}}
function dateToday(){return new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Moscow',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date())}
function plusDays(day,amount){const d=new Date(day+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+amount);return d.toISOString().slice(0,10)}
function period(days){
 const today=dateToday(),end=days===0?plusDays(today,-1):today;
 fromEl.value=days<=1?end:plusDays(end,1-days);toEl.value=end;
 load();
}
function metric(label,value){return '<div class="stat"><div class="small">'+esc(label)+'</div><div class="value">'+esc(value)+'</div></div>'}
function renderSummary(t){
 return metric('Собрано WB FBS',fmtN(t.orders))+
 metric('Средняя скорость',t.orders_per_hour==null?'н/д':t.orders_per_hour+' /ч')+
 metric('Время сборки',duration(t.picking_seconds))+
 metric('Пополнение, время',duration(t.replenishment_seconds))+
 metric('Пополнений',fmtN(t.replenishment_tasks))+
 metric('Пополнено, шт.',fmtN(t.replenishment_units));
}
function shiftDetails(x){
 return (x.shifts||[]).map(s=>'<div class="inner">'+
  esc(humanStamp(s.started_at))+' → '+esc(s.ended_at?humanStamp(s.ended_at):'не закрыта')+
  ' · '+esc(s.status==='open'?'открыта':'закрыта')+
  ' · FBS за день '+fmtN(s.orders)+'</div>').join('')||
  '<div class="small">Смены, пересекающиеся с этим днём, не найдены.</div>';
}
function actDetails(x){
 return (x.activities||[]).map(a=>'<div class="inner">'+esc(a.label)+
  ' · '+esc(humanStamp(a.started_at))+'–'+esc(humanStamp(a.ended_at))+
  ' · '+duration(a.seconds)+'</div>').join('')||
  '<div class="small">Нет записанных интервалов операций.</div>';
}
function taskDetails(x){
 return (x.tasks||[]).map(t=>'<div class="inner"><b>'+esc(t.article)+'</b> · '+
  esc(t.location)+' · '+fmtN(t.quantity)+' шт. · '+esc(humanStamp(t.completed_at))+'</div>').join('')||
  '<div class="small">Завершённых заданий нет.</div>';
}
function detailContent(x){
 return '<div class="details-grid">'+
  '<div><h2>Смены</h2>'+shiftDetails(x)+'<h2 style="margin-top:16px">Операции</h2>'+actDetails(x)+'</div>'+
  '<div><h2>Завершённые пополнения</h2>'+taskDetails(x)+
  ((x.warnings||[]).length?'<h2 style="margin-top:16px">Предупреждения</h2>'+
   x.warnings.map(w=>'<div class="warning">⚠ '+esc(w)+'</div>').join(''):'')+
  (x.debt_orders?'<p class="small">Из заказов FBS закрытие долга: '+fmtN(x.debt_orders)+'</p>':'')+
  '</div></div>';
}
function renderTable(rows){
 renderedRows=rows;
 if(!rows.length)return '<tr><td colspan="9" class="empty">По выбранным параметрам операций не найдено.</td></tr>';
 return rows.map((x,i)=>'<tr class="'+(x.warnings.length?'has-warning':'')+'">'+
 '<td>'+esc(humanDate(x.date))+'</td><td><b>'+esc(x.employee)+'</b>'+
 (x.warnings.length?'<div class="warning">⚠ Проверь данные</div>':'')+'</td>'+
 '<td class="num">'+fmtN(x.orders)+'</td>'+
 '<td class="num">'+(x.orders_per_hour==null?'н/д':esc(x.orders_per_hour))+'</td>'+
 '<td>'+duration(x.picking_seconds)+'</td><td>'+duration(x.replenishment_seconds)+'</td>'+
 '<td class="num">'+fmtN(x.replenishment_tasks)+'</td><td class="num">'+fmtN(x.shifts.length)+'</td>'+
 '<td><button type="button" class="details-button" data-index="'+i+'" aria-expanded="false">Подробнее</button></td>'+
 '</tr><tr class="details-row" id="details-'+i+'" hidden><td colspan="9">'+detailContent(x)+'</td></tr>').join('');
}
function updateEmployees(list){
 allEmployees=list||[];const prev=empEl.value;
 empEl.innerHTML='<option value="">Все сотрудники</option>'+
 allEmployees.map(x=>'<option value="'+esc(x.id)+'">'+esc(x.name)+(x.active?'':' (неактивен)')+'</option>').join('');
 if(allEmployees.some(e=>e.id===prev))empEl.value=prev;
}
async function load(){
 if(busy)return;
 if(!fromEl.value||!toEl.value||fromEl.value>toEl.value){statusEl.textContent='Укажи корректные даты.';statusEl.className='status err';return}
 const q=new URLSearchParams({from:fromEl.value,to:toEl.value});
 if(empEl.value)q.set('employee_id',empEl.value);
 busy=true;btn.disabled=true;statusEl.textContent='Загружаю историю…';statusEl.className='status';
 try{
  const resp=await fetch('/api/manager/shift-history?'+q.toString(),{cache:'no-store'});
  if(resp.status===403){location.href='/manager-login';return}
  const data=await resp.json();if(!resp.ok)throw Error(data.error||'Ошибка запроса');
  updateEmployees(data.employees);document.getElementById('summary').innerHTML=renderSummary(data.totals);
  document.getElementById('rows').innerHTML=renderTable(data.rows);
  statusEl.textContent='Период: '+humanDate(data.from)+' – '+humanDate(data.to)+
   ' · строк: '+data.rows.length+(data.totals.quality_warning?' · ⚠ Есть неполные данные':'');
 }catch(e){statusEl.textContent=e.message;statusEl.className='status err'}
 finally{busy=false;btn.disabled=false}
}
document.getElementById('rows').addEventListener('click',e=>{
 const b=e.target.closest('button[data-index]');if(!b)return;
 const row=document.getElementById('details-'+b.dataset.index);if(!row)return;
 row.hidden=!row.hidden;b.setAttribute('aria-expanded',String(!row.hidden));b.textContent=row.hidden?'Подробнее':'Свернуть';
});
btn.addEventListener('click',load);
document.querySelectorAll('button[data-days]').forEach(b=>b.addEventListener('click',()=>period(Number(b.dataset.days))));
fromEl.value=toEl.value=dateToday();load();
</script></body></html>"""
