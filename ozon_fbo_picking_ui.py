"""User interfaces for Ozon FBO scanner picking and manager progress."""

WORKER_PICK_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>ONIX · Ozon FBO · Сборка</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script><style>
*{box-sizing:border-box}body{margin:0;background:#e9eef3;color:#25313d;font-family:system-ui,Arial}
main{max-width:940px;margin:auto;padding:12px}.card{background:#f7f9fb;padding:16px;border:1px solid #d7e0e7;border-radius:14px;margin:10px 0}
a{color:#466b85}h1{font-size:25px;margin:12px 0 6px}h2{font-size:19px;margin:4px 0 12px}
.small{font-size:13px;color:#647585}.error{color:#a43838;font-weight:750}.ok{color:#267549;font-weight:750}
button{border:0;border-radius:9px;padding:12px;background:#48647a;color:white;font-size:15px;font-weight:800;cursor:pointer}
button:disabled{opacity:.45;cursor:wait}button.secondary{background:#dae4eb;color:#25313d}
input{padding:12px;font-size:18px;max-width:100%;border:1px solid #b9cbd7;border-radius:10px;background:white}
input[type=number]{width:125px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:9px}
.metric{background:#e9f0f4;padding:12px;border-radius:10px}.metric b{display:block;font-size:25px}
.metric span{font-size:12px;color:#637484}.progress{height:13px;border-radius:20px;background:#d5e1e8;overflow:hidden;margin-top:12px}
.progress>div{background:#567e92;height:100%;transition:width .25s}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.full{width:100%}
label{font-size:13px;font-weight:800;display:block;margin-bottom:5px}
.actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;min-width:480px}td,th{border-bottom:1px solid #dde7eb;text-align:left;padding:9px;font-size:13px}
th{background:#e8eef2;position:sticky;top:0}.scroll{overflow:auto;max-height:48vh}
.complete{background:#dcefe3}.hidden{display:none!important}
#reader{margin-top:12px;max-width:490px;overflow:hidden;border-radius:12px}
#message{padding:12px;background:#eef3f6;border-radius:9px;min-height:42px;overflow-wrap:anywhere}
@media(max-width:620px){.grid{grid-template-columns:1fr}.metrics{gap:5px}.metric{padding:9px}.metric b{font-size:20px}}
</style></head><body><main>
<a href="/ozon-fbo">← Все грузоместа Ozon FBO</a><h1>📦 Сборка Ozon FBO</h1>
<p class="small" id="identity">Определяю сотрудника…</p>
<section class="card"><h2 id="cargoName">Загружаю грузоместо…</h2>
<div class="small" id="cargoMeta"></div>
<div class="metrics" style="margin-top:13px"><div class="metric"><span>План</span><b id="planned">—</b></div>
<div class="metric"><span>Собрано</span><b id="picked">—</b></div>
<div class="metric"><span>Осталось</span><b id="remaining">—</b></div></div>
<div class="progress"><div id="bar" style="width:0"></div></div>
<div id="owner" class="small" style="margin:12px 0"></div>
<button id="claimBtn" class="hidden">Начать сборку грузоместа</button></section>
<section class="card hidden" id="workCard">
<h2>1. Сканируй товар</h2>
<div class="grid">
 <div><label for="barcode">Штрихкод товара (Zebra/HID)</label><input id="barcode" class="full" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Сканируй товар"></div>
 <div><label for="quantity">Сколько взято, шт.</label><input id="quantity" type="number" step="1" min="1" value="1"></div>
</div>
<div id="selected" class="small" style="padding:12px 0">Сначала сканируй товар.</div>
<div class="actions"><button id="addBtn">Подтвердить количество</button>
<button id="undoBtn" class="secondary">Отменить последний скан</button>
<button id="cameraProduct" class="secondary">📷 Камера товара</button></div>
<div id="reader" class="hidden"></div>
<p class="small">Скан товара без подтверждения количества не изменяет план. При сбое сети незавершённое подтверждение будет повторено безопасно.</p>
</section>
<section class="card hidden complete" id="finishCard">
<h2>2. Подтверди готовое грузоместо</h2>
<p class="small">Все позиции собраны. Отсканируй ШК грузоместа с его этикетки, чтобы завершить задание.</p>
<div class="grid"><input id="cargoCode" class="full" autocomplete="off" autocapitalize="none" placeholder="ШК грузоместа">
<button id="finishBtn">Завершить грузоместо</button></div>
<p><button class="secondary" id="cameraCargo">📷 Камера ШК грузоместа</button></p>
</section>
<div id="message" role="status" aria-live="polite">Готовлю сборку…</div>
<section class="card"><h2>Позиции этого грузоместа</h2>
<div class="scroll"><table><thead><tr><th>Артикул / ШК</th><th>План</th><th>Собрано</th><th>Осталось</th></tr></thead>
<tbody id="items"></tbody></table></div></section>
<script>
const E=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const code=s=>{s=String(s||'').trim();return /^\\][A-Za-z]\\d/.test(s)?s.slice(3):s};
const endpoint='/api/ozon-fbo/pick', query=new URLSearchParams(location.search);
const cargoId=query.get('cargo_id');let cargo=null,employee=null,busy=false,verifiedItem=null;
const pendingKey='onix_fbo_pending_scan_v1';
const pendingUndoKey='onix_fbo_pending_undo_v1';
let camera=null,cameraTarget='',cameraBusy=false;
async function stopCamera(){
 const active=camera;camera=null;cameraBusy=false;
 if(active){try{await active.stop()}catch(e){}try{await active.clear()}catch(e){}}
 E('reader').classList.add('hidden');
}
async function toggleCamera(target){
 if(camera){await stopCamera();return}
 if(typeof Html5Qrcode==='undefined'){status('Модуль камеры не загрузился. Используй HID-сканер или ручной ввод ШК.',true);return}
 cameraTarget=target;E('reader').classList.remove('hidden');
 const instance=new Html5Qrcode('reader');camera=instance;
 try{
  await instance.start({facingMode:'environment'},{fps:12,qrbox:{width:260,height:160}},
   async value=>{
    if(camera!==instance||cameraBusy)return;
    cameraBusy=true;
    await stopCamera();
    const scanned=code(value);
    if(target==='item'){E('barcode').value=scanned;verifyBarcode()}
    else{E('cargoCode').value=scanned;status('ШК грузоместа считан. Подтверди завершение.')}
   },()=>{});
 }catch(e){await stopCamera();status('Не удалось открыть камеру: '+e.message,true)}
}
window.addEventListener('pagehide',()=>{if(camera)stopCamera()});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&camera)stopCamera()});
function status(s,error=false){E('message').className=error?'error':'small';E('message').textContent=s}
function vibrate(ok){try{if(navigator.vibrate)navigator.vibrate(ok?70:[120,70,120])}catch(e){}}
function makeId(){return crypto.randomUUID?crypto.randomUUID(): 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const r=crypto.getRandomValues(new Uint8Array(1))[0]&15;return (c==='x'?r:(r&3|8)).toString(16)})}
async function req(path,method='GET',data){
 const r=await fetch(endpoint+path,{method,cache:'no-store',headers:{'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined});
 const d=await r.json();if(!r.ok){const e=new Error(d.error||'Ошибка сервера');e.status=r.status;e.active=d.active_cargo_id;throw e}
 return d;
}
async function draw(c){
 cargo=c;
 E('cargoName').textContent=c.supply_title+' · '+c.destination;
 E('cargoMeta').textContent=c.label+' · ШК грузоместа '+c.barcode+' · Статус: '+({planned:'План',started:'Сборка',done:'Готово'}[c.status]||c.status);
 E('planned').textContent=c.qty;E('picked').textContent=c.picked;E('remaining').textContent=c.remaining;
 E('bar').style.width=(c.qty?Math.min(100,100*c.picked/c.qty):0).toFixed(1)+'%';
 const own=c.employee_id===employee.id;
 E('owner').textContent=c.status==='done'?'Грузоместо полностью собрано · '+(c.employee||''):c.employee_id?
 ('Исполнитель: '+(c.employee||c.employee_id)+(own?' (ты)':'')):'Грузоместо свободно';
 E('claimBtn').classList.toggle('hidden',c.status==='done'||!!c.employee_id);
 E('workCard').classList.toggle('hidden',c.status!=='started'||!own);
 E('finishCard').classList.toggle('hidden',c.status!=='started'||!own||c.remaining!==0);
 E('items').innerHTML=c.items.map(i=>'<tr><td><b>'+esc(i.article)+'</b><div class="small">'+esc(i.barcode)+'</div></td><td>'+esc(i.qty)+'</td><td>'+esc(i.picked)+'</td><td>'+esc(i.remaining)+'</td></tr>').join('');
 E('undoBtn').disabled=!c.picked||!own||c.status!=='started';
 if(c.status==='done')status('Грузоместо собрано и подтверждено. Можно переходить к следующему.',false);
 if(verifiedItem){
  const match=c.items.find(i=>i.barcode===verifiedItem.barcode);
  if(!match||!match.remaining){verifiedItem=null;E('selected').textContent='Сканируй следующий товар.';E('barcode').value='';}
  else{verifiedItem=match;E('selected').textContent=match.article+' · осталось '+match.remaining+' шт.';E('quantity').max=match.remaining}
 }
}
async function refresh(){
 const d=await req('/cargo/'+encodeURIComponent(cargoId));await draw(d.cargo);
}
function verifyBarcode(){
 if(!cargo||!employee)return;
 const barcode=code(E('barcode').value),item=cargo.items.find(i=>i.barcode===barcode);
 if(!item){verifiedItem=null;status('Неверный товар: этого штрихкода нет в задании.',true);vibrate(false);E('barcode').value='';E('barcode').focus();return}
 if(!item.remaining){verifiedItem=null;status('Этот артикул уже собран полностью.',true);vibrate(false);E('barcode').value='';E('barcode').focus();return}
 // A new SKU always starts at 1. Do not carry over quantity from a previous SKU.
 if(!verifiedItem||verifiedItem.barcode!==item.barcode)E('quantity').value=1;
 verifiedItem=item;E('selected').textContent=item.article+' · необходимо ещё '+item.remaining+' шт.';
 E('quantity').max=item.remaining;
 status('Товар верный. Укажи фактическое количество и подтверди.');vibrate(true);E('quantity').focus();
}
E('cameraProduct').addEventListener('click',()=>toggleCamera('item'));
E('cameraCargo').addEventListener('click',()=>toggleCamera('cargo'));
E('barcode').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();verifyBarcode()}});
E('barcode').addEventListener('change',verifyBarcode);
E('claimBtn').addEventListener('click',async()=>{
 if(busy)return;busy=true;E('claimBtn').disabled=true;
 try{const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/claim','POST',{});await draw(d.cargo);status('Грузоместо закреплено. Сканируй товар.');E('barcode').focus()}
 catch(e){status(e.message+(e.active?' Вернись к уже начатому грузоместу.':''),true)}
 finally{busy=false;E('claimBtn').disabled=false}
});
function getPending(){try{return JSON.parse(localStorage.getItem(pendingKey)||'null')}catch(e){return null}}
function getPendingUndo(){try{return JSON.parse(localStorage.getItem(pendingUndoKey)||'null')}catch(e){return null}}
async function sendPending(p){
 const d=await req('/cargo/'+encodeURIComponent(p.cargo_id)+'/scan','POST',{
  barcode:p.barcode,quantity:p.quantity,request_id:p.request_id
 });
 localStorage.removeItem(pendingKey);verifiedItem=null;E('barcode').value='';
 E('quantity').value=1;E('selected').textContent='Сканируй следующий товар.';
 await draw(d.cargo);status((d.replayed?'Уже сохранено: ':'Принято: ')+p.quantity+' шт.');vibrate(true);
 E('barcode').focus();return d;
}
async function recoverPending(){
 const p=getPending();
 if(!p)return;
 if(p.employee_id!==employee.id||p.cargo_id!==cargoId){
  status('Есть незавершённое подтверждение на другом грузоместе. Открой его для восстановления.',true);return;
 }
 busy=true;E('addBtn').disabled=true;
 status('Проверяю последнее подтверждение после обрыва связи…');
 try{await sendPending(p)}
 catch(e){
  if(e.status && e.status<500){localStorage.removeItem(pendingKey);status(e.message,true)}
  else status('Сеть не ответила. Последнее подтверждение сохранено для безопасного повтора. Обнови страницу после восстановления связи.',true)
 }finally{busy=false;E('addBtn').disabled=false}
}
E('addBtn').addEventListener('click',async()=>{
 if(busy||!cargo||cargo.status!=='started'||cargo.employee_id!==employee.id)return;
 if(getPending()){await recoverPending();return}
 verifyBarcode();if(!verifiedItem)return;
 const qty=Number(E('quantity').value);
 if(!Number.isSafeInteger(qty)||qty<1||qty>verifiedItem.remaining){status('Укажи целое количество от 1 до '+verifiedItem.remaining,true);return}
 const p={cargo_id:cargoId,employee_id:employee.id,barcode:verifiedItem.barcode,quantity:qty,request_id:makeId()};
 localStorage.setItem(pendingKey,JSON.stringify(p));
 busy=true;E('addBtn').disabled=true;
 try{await sendPending(p)}
 catch(e){
  if(e.status && e.status<500){localStorage.removeItem(pendingKey);status(e.message,true);vibrate(false)}
  else{status('Нет подтверждения сервера. Повторный скан пока НЕ делай. При восстановлении сети операция безопасно повторится.',true)}
 }finally{busy=false;E('addBtn').disabled=false}
});
async function recoverUndo(){
 const pending=getPendingUndo();if(!pending)return;
 if(pending.employee_id!==employee.id){localStorage.removeItem(pendingUndoKey);return}
 if(pending.cargo_id!==cargoId){status('Незавершённая отмена относится к другому грузоместу.',true);return}
 busy=true;E('undoBtn').disabled=true;
 try{
  const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/undo','POST',{request_id:pending.request_id});
  localStorage.removeItem(pendingUndoKey);
  verifiedItem=null;E('barcode').value='';await draw(d.cargo);
  status(d.replayed?'Отмена уже была сохранена.':'Последнее подтверждение отменено.');
 }catch(e){
  if(e.status&&e.status<500){localStorage.removeItem(pendingUndoKey);status(e.message,true)}
  else status('Неизвестен результат отмены. Не нажимай повторно: запрос восстановится автоматически.',true);
 }finally{busy=false;E('undoBtn').disabled=false}
}
E('undoBtn').addEventListener('click',async()=>{
 if(busy||getPending())return status('Сначала восстанови последнее подтверждение.',true);
 if(getPendingUndo()){await recoverUndo();return}
 if(!confirm('Отменить последнее подтверждённое количество в этом грузоместе?'))return;
 localStorage.setItem(pendingUndoKey,JSON.stringify({
  cargo_id:cargoId,employee_id:employee.id,request_id:makeId()
 }));
 await recoverUndo();
});
E('cargoCode').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();E('finishBtn').click()}});
E('finishBtn').addEventListener('click',async()=>{
 if(busy||getPending())return status('Сначала восстанови последнее подтверждение.',true);
 const scanned=code(E('cargoCode').value);if(!scanned)return status('Сканируй ШК грузоместа.',true);
 if(!confirm('Все '+cargo.qty+' шт. собраны. Завершить грузоместо после проверки ШК?'))return;
 busy=true;E('finishBtn').disabled=true;
 try{const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/complete','POST',{cargo_barcode:scanned});await draw(d.cargo)}
 catch(e){status(e.message,true)}finally{busy=false;E('finishBtn').disabled=false}
});
async function startup(){
 const response=await fetch('/api/auth/me',{cache:'no-store'});
 if(!response.ok){location.href='/login';return}
 const d=await response.json();employee=d.employee;E('identity').textContent='Сотрудник: '+employee.name;
 for(const key of [pendingKey,pendingUndoKey]){
  const p=(()=>{try{return JSON.parse(localStorage.getItem(key)||'null')}catch(e){return null}})();
  if(p&&p.employee_id!==employee.id)localStorage.removeItem(key);
  else if(p&&p.cargo_id!==cargoId){
   location.replace('/ozon-fbo/pick?cargo_id='+encodeURIComponent(p.cargo_id));return;
  }
 }
 if(!cargoId){
  const active=await req('/active');
  if(active.active){location.replace('/ozon-fbo/pick?cargo_id='+encodeURIComponent(active.cargo_id));return}
  location.replace('/ozon-fbo');return;
 }
 await refresh();
 await recoverPending();
 await recoverUndo();
}
startup().catch(e=>status('Ошибка подключения: '+e.message+'. При восстановлении сети обнови страницу.',true));
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&employee&&cargoId&&!busy)refresh().catch(()=>{})});
</script></main></body></html>"""


MANAGER_PICK_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Монитор сборки Ozon FBO</title>
<meta name="theme-color" content="#09121e"><style>
:root{color-scheme:dark;--bg:#09121e;--panel:#122438;--panel-deep:#101e2d;--line:#314d64;--text:#eef6ff;--muted:#a4bbce;--blue:#2b92e5;--orange:#ff983b;--green:#55d3a1}
*{box-sizing:border-box}
body{font:14px/1.48 system-ui,-apple-system,"Segoe UI",Arial,sans-serif;margin:0;background:var(--bg);color:var(--text);-webkit-font-smoothing:antialiased}
main{max-width:1600px;margin:0 auto;padding:24px 28px 40px}
a{color:#77c0ff;text-decoration:none}a:hover{text-decoration:underline}
.back{display:inline-flex;gap:8px;align-items:center;font-size:13px;margin-bottom:13px}
h1{font-size:clamp(23px,2vw,30px);line-height:1.25;margin:0 0 8px;font-weight:760;letter-spacing:-.55px}
h2{font-size:18px;margin:0 0 13px;letter-spacing:-.2px}
.heading{display:flex;align-items:center;gap:12px}.heading svg{width:29px;height:29px;color:#55b7f9;flex:none}
.small{color:var(--muted);font-size:12.5px}
.intro{margin:0 0 22px;font-size:13px}
.card{background:linear-gradient(140deg,#14273b,#0f1e2e);border:1px solid var(--line);border-radius:13px;padding:18px;margin:15px 0;box-shadow:0 6px 16px rgba(0,0,0,.08)}
.monitor-card{padding:12px 15px 15px}
.monitor-title{display:flex;align-items:center;gap:10px;margin:3px 0 14px;padding:0 4px}
.monitor-title svg{width:20px;height:20px;color:#78bfff;flex:none}
.monitor-title h2{font-size:16px;margin:0}
.monitor-title .small{margin-left:auto;text-align:right}
table{width:100%;border-collapse:collapse;min-width:930px;background:transparent}
th,td{padding:12px 14px;border-bottom:1px solid #294158;text-align:left;font-size:13px;vertical-align:middle}
th{background:#1b3449;color:#c9e0f0;font-weight:730;font-size:12px;letter-spacing:.08px;white-space:nowrap}
td{color:#e6f0fa}td b{font-weight:730;color:#f6faff}td .small{font-size:11.5px;margin-top:4px}
tbody tr{background:transparent;transition:background .12s}tbody tr:hover{background:#1e364a}
tbody tr.is-done{background:rgba(47,127,101,.07)}tbody tr.is-done:hover{background:rgba(47,127,101,.17)}
tbody tr.is-done td:first-child{box-shadow:inset 3px 0 0 #37b889}
tbody tr.is-done td{border-bottom-color:#2e4e50}
.scroll{overflow-x:auto;scrollbar-color:#3b5d76 #0e1c2b}
button{border:1px solid #438ac1;background:#176eae;color:#f7fbff;padding:9px 13px;border-radius:9px;min-height:37px;cursor:pointer;font-size:12.5px;font-weight:700;white-space:nowrap}
button:hover{background:#2488cf}
button:disabled{opacity:.5;cursor:wait}
button.secondary{background:#203a50;color:#cae7ff;border:1px solid #365976}
button.secondary:hover{background:#2a516c}
button:focus-visible,a:focus-visible{outline:2px solid var(--orange);outline-offset:2px}
.progress{background:#294257;border-radius:25px;height:9px;overflow:hidden;min-width:115px;margin-top:7px}
.progress>div{background:#6b89a2;height:100%;transition:width .2s}
.progress.is-started>div{background:linear-gradient(90deg,#1b80c8,#4ab5f6)}
.progress.is-done>div{background:linear-gradient(90deg,#238968,#57d6a0)}
.status-badge{display:inline-flex;align-items:center;min-height:28px;padding:5px 11px;border-radius:7px;font-size:11.5px;font-weight:750;white-space:nowrap}
.status-badge.is-planned{background:#26394a;color:#b1c9d9;border:1px solid #3f586a}
.status-badge.is-started{background:#173c5b;color:#91d1ff;border:1px solid #275c83}
.status-badge.is-done{background:#173e36;color:#81e9b9;border:1px solid #2c6a57}
#audit{font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere;margin-top:12px;line-height:1.75;color:#d5e6f5;min-height:48px}
.audit-card{border-left:3px solid #2c97dc}
.meta-label{color:#9ebad0;font-size:12px;margin-left:6px}
@media(max-width:800px){main{padding:15px}.card{padding:12px}.monitor-card{padding:10px}.monitor-title .small{display:none}th,td{padding:10px}}
@media(prefers-reduced-motion:reduce){.progress>div,tbody tr{transition:none}}
</style></head><body><main><a class="back" href="/manager/ozon-fbo">← Поставки Ozon FBO</a>
<h1 class="heading"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 2 9 5v10l-9 5-9-5V7zM3 7l9 5 9-5M12 12v10"/></svg>Монитор сборки Ozon FBO</h1><p class="small intro">Подтверждённые сканирования · Обновление каждые 15 секунд · Без списаний в МойСклад</p>
<div class="card monitor-card"><div class="monitor-title"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 18h3v-7H3zM10 18h3V6h-3zM17 18h3V3h-3z"/></svg><h2>Грузоместа и прогресс сборки</h2><span class="small">Фактические данные Ozon FBO</span></div><div class="scroll"><table><thead><tr><th>Поставка / направление</th><th>ШК грузоместа</th><th>Исполнитель</th><th>Собрано</th><th>Статус</th><th>Действия</th></tr></thead>
<tbody id="rows"><tr><td colspan="6">Загрузка…</td></tr></tbody></table></div></div>
<div class="card audit-card"><h2>История грузоместа</h2><div id="audit" class="small">Нажми «История» в строке грузоместа.</div></div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let list=[];
function progressState(c){return c.status==='done'?'is-done':c.status==='started'?'is-started':'is-planned'}
function statusLabel(c){return ({planned:'План',started:'Собирается',done:'Собрано'}[c.status]||c.status)}
async function load(){
 const r=await fetch('/api/manager/ozon-fbo/progress',{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Не удалось загрузить');
 list=d.cargo||[];
 document.getElementById('rows').innerHTML=list.length?list.map(c=>
 '<tr class="'+progressState(c)+'"><td><b>'+esc(c.supply)+'</b><div class="small">'+esc(c.destination)+' · '+esc(c.label)+'</div></td>'+
 '<td>'+esc(c.barcode)+'</td><td>'+esc(c.employee||'—')+'</td>'+
 '<td>'+esc(c.picked)+' / '+esc(c.qty)+'<div class="progress '+progressState(c)+'"><div style="width:'+Math.min(100,c.qty?100*c.picked/c.qty:0)+'%"></div></div></td>'+
 '<td><span class="status-badge '+progressState(c)+'">'+(c.status==='done'?'✓ ':'')+esc(statusLabel(c))+'</span></td>'+
 '<td><button data-history="'+esc(c.id)+'" class="secondary">История</button> '+
 (c.status!=='done'&&c.employee_id?'<button data-release="'+esc(c.id)+'">Передать</button>':'')+'</td></tr>').join(''):
 '<tr><td colspan="6">Нет поставок.</td></tr>';
}
document.getElementById('rows').addEventListener('click',async e=>{
 const history=e.target.closest('button[data-history]'),release=e.target.closest('button[data-release]');
 if(history){
  const r=await fetch('/api/manager/ozon-fbo/audit/'+encodeURIComponent(history.dataset.history)),d=await r.json();
  if(!r.ok)return document.getElementById('audit').textContent=d.error||'Ошибка';
  document.getElementById('audit').textContent=[
   'Действия:',...(d.actions||[]).map(a=>a.at+' · '+a.actor+' · '+a.action+' · '+a.reason),
   '', 'Сканы:',...(d.scans||[]).map(s=>s.at+' · '+s.employee+' · ШК '+s.barcode+' · '+s.qty+' шт.'+(s.voided?' (отменён)':''))
  ].join('\\n');return;
 }
 if(release){
  const cargo=list.find(x=>x.id===release.dataset.release);
  const reason=prompt('Почему передаём грузоместо '+(cargo?cargo.barcode:'')+' другому сотруднику?');
  if(reason===null)return;
  if(reason.trim().length<8)return alert('Причина должна содержать хотя бы 8 символов.');
  if(!confirm('Снять исполнителя? Ранее собранные количества сохранятся.'))return;
  release.disabled=true;
  try{
   const r=await fetch('/api/manager/ozon-fbo/release',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({cargo_id:release.dataset.release,reason:reason.trim()})});
   const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка передачи');
   await load();
  }catch(e){alert(e.message)}finally{release.disabled=false}
 }
});
load().catch(e=>document.getElementById('rows').innerHTML='<tr><td colspan="6">'+esc(e.message)+'</td></tr>');
setInterval(()=>{if(!document.hidden)load().catch(()=>{})},15000);
</script></main></body></html>"""
