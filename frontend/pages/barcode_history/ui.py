"""Barcode history presentation; preserve the existing HTML contract."""

HISTORY_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#e9eef3"><title>GRIT WMS · История ШК</title>
<style>
*{box-sizing:border-box}body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#e9eef3;margin:0;color:#25313d}
.wrap{max-width:1450px;margin:auto;padding:14px}.card{background:#f7f9fb;border:1px solid #d7e0e7;border-radius:16px;padding:16px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;align-items:center;gap:10px}.top a{padding:9px 12px;border-radius:9px;background:#dde8ef;color:#314657;text-decoration:none;font-weight:750}
h1{margin:0;font-size:24px}.small{font-size:12px;color:#6b7785}.search{display:grid;grid-template-columns:1fr auto;gap:8px;margin-top:12px}
input,button{min-height:44px;border:1px solid #cbd9e2;border-radius:9px;padding:9px 11px;font-size:15px}button{background:#48647a;color:#fff;font-weight:800;cursor:pointer}
.timeline{margin-top:12px}.event{display:grid;grid-template-columns:155px 130px 160px minmax(0,1fr);gap:10px;padding:11px 0;border-bottom:1px solid #e1e7eb;align-items:center}.event:last-child{border:0}
.time{font-size:12px;color:#6b7785}.who{font-weight:800}.type{font-weight:800}.meta{font-size:12px}.empty{padding:22px 0;color:#6b7785}
@media(max-width:800px){.event{grid-template-columns:1fr 1fr}.meta{grid-column:1/-1}.top{align-items:flex-start;flex-direction:column}}
</style><link rel="icon" type="image/svg+xml" href="/favicon.svg?v=2"></head><body><div class="wrap">
<div class="card">
 <div class="top"><div><h1>🔎 История ШК</h1><div class="small">Сканируй или введи ШК товара, WB, SSCC, артикул или код объекта.</div></div><a href="/manager">← Панель управления</a></div>
 <div class="search"><input id="q" autofocus placeholder="ШК / WB / SSCC / артикул"><button onclick="load()">Найти</button></div>
 <div id="summary" class="small" style="margin-top:9px"></div>
 <div id="timeline" class="timeline"><div class="empty">Введи код объекта.</div></div>
</div>
</div>
<script>
function esc(s){return String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));}
function fmt(ts){if(!ts)return '—';try{return new Date(ts).toLocaleString('ru-RU')}catch(e){return ts}}
function label(t){return ({scan:'Приёмка / скан',external_pallet:'ОСЗ / SSCC',complete:'Документ завершён',start:'Начало смены',end:'Конец смены',switch:'Смена операции',duplicate_wb:'Повторный WB',wb_same_as_product:'ШК товара вместо WB',product_not_found:'Товар не найден',product_mismatch:'Не тот товар',scan_rejected:'Скан отклонён',fbs_scan:'FBS заказ'})[t]||t||'Событие'}
async function load(){
 const q=document.getElementById('q').value.trim();if(!q)return;
 const r=await fetch('/api/history?q='+encodeURIComponent(q));if(r.status===403){location.href='/manager-login';return}
 const d=await r.json();const box=document.getElementById('timeline');
 document.getElementById('summary').textContent='Найдено событий: '+d.rows.length;
 box.innerHTML=d.rows.length?d.rows.map(x=>'<div class="event"><div class="time">'+esc(fmt(x.created_at))+'</div><div class="who">'+esc(x.employee||'—')+'</div><div class="type">'+esc(label(x.event_type))+'</div><div class="meta"><b>'+esc(x.article||x.object_code||'—')+'</b> · '+esc(x.product_name||x.operation_type||'')+(x.quantity!=null?' · '+esc(x.quantity)+' шт.':'')+(x.result?' · '+esc(x.result):'')+'</div></div>').join(''):'<div class="empty">По этому коду история не найдена.</div>';
}
document.getElementById('q').addEventListener('keydown',e=>{if(e.key==='Enter')load()});
</script></body></html>"""
