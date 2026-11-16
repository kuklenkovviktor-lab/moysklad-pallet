"""User interfaces for Ozon FBO scanner picking and manager progress."""

WORKER_PICK_HTML = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#09121e"><title>GRIT WMS · Ozon FBO · Сборка</title>
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

/* Shared GRIT WMS dark UI: Ozon FBO employee terminal */
:root{color-scheme:dark;--g-bg:#09121e;--g-panel:#142337;--g-line:#30465c;--g-text:#f1f6ff;--g-muted:#9eb5ca;--g-orange:#ff9230}
html,body{background:var(--g-bg);color:var(--g-text)}
.card{background:linear-gradient(145deg,#16283b,#101c2a);border:1px solid var(--g-line);color:var(--g-text)}
a{color:#77c0ff}h1,h2{color:#f1f6ff}
.small{color:var(--g-muted)}.error{color:#ff999c}.ok{color:#81e9b9}
button{background:#176eae;border:1px solid #358acc;color:#fff}
button:hover{background:#2488cf}
button.secondary{background:#233b50;color:#e0f0ff;border:1px solid #375a75}
input,select{background:#0d1d2c;color:#ecf5ff;border:1px solid #42607a}
input::placeholder{color:#819bb1}
.metric{background:#1b3045;border:1px solid #345069}
.metric span{color:#9eb5ca}
.progress{background:#283e54}.progress>div{background:linear-gradient(90deg,#288ce0,#ff942f)}
td,th{border-color:#2b4255}th{background:#1b3146;color:#b8d7ed}
.complete{background:linear-gradient(145deg,#173b35,#11272a);border-color:#2e705b}
#message{background:#1b2d40;border:1px solid #415e78;color:#f1f6ff}
input:focus-visible,select:focus-visible,button:focus-visible,a:focus-visible{outline:2px solid var(--g-orange);outline-offset:2px}
/* Mobile picking: completed positions are visibly distinct and move below unfinished ones. */
#items tr.position-done{background:#173b35;color:#a4dfc9}
#items tr.position-done td{border-color:#2e705b}
#items tr.position-done b{color:#a4dfc9}
#items tr.position-pending{background:#142337;color:#f1f6ff}
.position-state{display:inline-block;width:22px;color:#ff9230;font-size:18px;font-weight:800}
.position-done .position-state{color:#81e9b9}
.pick-location{display:inline-block;font-weight:850;font-size:16px;color:#ffb66f;padding-top:3px}
.pick-location.unavailable{font-size:12px;font-weight:600;color:#9eb5ca}
.pick-sort{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:7px 0}
.pick-sort select{padding:9px;border-radius:8px;background:#102337;color:#ecf5ff;border:1px solid #42607a}

@media(max-width:620px){#items td{padding:10px 6px;font-variant-numeric:tabular-nums}}
/* GRIT focused one-position terminal: single work view on narrow phones. */
.compact-head{padding:11px 12px;margin:7px 0}.compact-head h2{font-size:17px;margin:0 0 4px}
.compact-head .metrics{margin:8px 0}.compact-head .metric{padding:7px}.compact-head .metric b{font-size:19px}
.compact-head .progress{height:6px;margin-top:7px}.compact-head #owner{margin:6px 0 0!important}
.active-card{margin:8px 0;padding:15px 13px;border-color:#47769f;background:linear-gradient(140deg,#193651,#10283e)}
.active-header{display:flex;justify-content:space-between;gap:8px;color:#a3c1da;font-size:12px}
.active-location{font-size:30px;font-weight:850;letter-spacing:.4px;color:#ffb66f;margin:8px 0}
.active-article{font-size:21px;overflow-wrap:anywhere;margin:3px 0 8px}
.active-quantity{font-size:17px;font-weight:700}
.scan-card{padding:12px;margin:8px 0}.scan-card .scan-fields{grid-template-columns:minmax(0,1fr) 110px;gap:8px}
.scan-card input{min-width:0;width:100%}.scan-card #quantity{width:100%}
.primary-action{min-height:48px}.other-positions,.extra-panel{padding:12px;margin:8px 0}
.other-positions summary,.extra-panel summary{cursor:pointer;font-weight:750;font-size:14px}
.position-list{display:grid;gap:6px}.position-choice{width:100%;text-align:left;background:#14283b;border:1px solid #34536d;padding:11px}
.position-choice.current{border-color:#ffb66f;background:#20384d}
.position-choice.done{border-color:#367a68;background:#173b35;color:#9fe0c6}
.position-choice b{display:block;font-size:14px}.position-choice span{color:#ffba78;font-size:13px}
.extra-panel #shortageCard{margin:14px 0 0}.extra-panel select{padding:10px;border-radius:8px;margin:5px 0 12px}
#message{padding:8px 11px;min-height:0;font-size:13px}
@media(max-width:420px){main{padding:8px}.scan-card label{font-size:12px}.active-location{font-size:28px}.active-article{font-size:20px}}
</style></head><body><main>
<a href="/ozon-fbo">← Все грузоместа Ozon FBO</a><h1>📦 Сборка Ozon FBO</h1>
<p class="small" id="identity">Определяю сотрудника…</p>
<section class="card compact-head">
<h2 id="cargoName">Загружаю грузоместо…</h2><div class="small" id="cargoMeta"></div>
<div class="metrics"><div class="metric"><span>План</span><b id="planned">—</b></div><div class="metric"><span>Собрано</span><b id="picked">—</b></div><div class="metric"><span>Осталось</span><b id="remaining">—</b></div></div>
<div class="progress"><div id="bar" style="width:0"></div></div>
<div id="owner" class="small"></div>
<button id="claimBtn" class="hidden full">Начать сборку грузоместа</button></section>
<section class="card active-card" id="activeCard">
<div class="active-header"><span>Следующий товар · крупные сначала</span><span id="positionStep">—</span></div>
<div id="activeLocation" class="active-location">Определяю ячейку…</div>
<h2 id="activeArticle" class="active-article">Загружаю позицию…</h2>
<div id="activeQuantity" class="active-quantity">—</div>
<p class="small" id="guideState">Подсказки загружаются. Сканирование не блокируется.</p>
</section>
<section class="card hidden scan-card" id="workCard">
<div class="grid scan-fields">
<div><label for="barcode">Штрихкод товара</label><input id="barcode" class="full" autocomplete="off" autocapitalize="none" spellcheck="false" placeholder="Сканируй товар"></div>
<div><label for="quantity">Количество, шт.</label><input id="quantity" type="number" step="1" min="1" value="1"></div></div>
<div id="selected" class="small" style="padding:7px 0">Сканируй товар.</div>
<button id="addBtn" class="full primary-action">Подтвердить количество</button>
<div class="actions" style="margin-top:7px"><button id="cameraProduct" class="secondary">📷 Камера товара</button></div>
<div id="reader" class="hidden"></div>
</section>
<div id="message" role="status" aria-live="polite">Загрузка задания…</div>
<details class="card other-positions" id="otherPositions"><summary>Другие позиции · выбор вручную <span id="positionsCount"></span></summary>
<div class="small" style="margin:9px 0">Нажми на товар, чтобы посмотреть его адрес. Сканировать можно любой ШК задания независимо от выбора.</div>
<div class="scroll"><div id="items" class="position-list"></div></div>
</details>
<details class="card extra-panel" id="extraActions"><summary>Дополнительные действия</summary>
<div class="actions" style="margin-top:12px"><button id="undoBtn" class="secondary">Отменить последний скан</button></div>
<section id="shortageCard" class="hidden">
<h2>Не найден товар</h2>
<p class="small">Недостача требует подтверждения руководителя и не меняет план автоматически.</p>
<label for="shortageItem">Позиция</label><select id="shortageItem" class="full"></select>
<label for="shortageReason">Причина</label><select id="shortageReason" class="full">
<option>Товар не найден в ячейке</option><option>Фактический остаток меньше плана</option><option>Товар повреждён, требуется проверка</option>
</select><button id="shortageBtn" class="full">Отправить недостачу</button>
<div id="shortageStatus" class="small" style="margin-top:10px"></div>
</section></details>
<section class="card hidden complete" id="finishCard">
<h2>Грузоместо собрано</h2><p class="small" id="finishHint">Отсканируй ШК грузоместа для завершения.</p>
<input id="cargoCode" class="full" autocomplete="off" autocapitalize="none" placeholder="ШК грузоместа">
<button id="finishBtn" class="full primary-action">Завершить грузоместо</button>
<button class="secondary" id="cameraCargo">📷 Камера ШК грузоместа</button>
</section>
<script>
const E=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const code=s=>{s=String(s||'').trim();return /^\\][A-Za-z]\\d/.test(s)?s.slice(3):s};
const endpoint='/api/ozon-fbo/pick', query=new URLSearchParams(location.search);
const cargoId=query.get('cargo_id');let cargo=null,employee=null,busy=false,verifiedItem=null;
let pickGuide={},recommendedIds=[],pickGuideReady=false,activeItemId=null;
const pendingKey='onix_fbo_pending_scan_v1';
const pendingUndoKey='onix_fbo_pending_undo_v1';
const pendingShortageKey='grit_fbo_pending_shortage_v1';
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
 E('owner').textContent=c.status==='done'?(c.approved_shortage?'Грузоместо закрыто с согласованной недостачей '+c.approved_shortage+' шт. · ':'Грузоместо полностью собрано · ')+(c.employee||''):c.employee_id?
 ('Исполнитель: '+(c.employee||c.employee_id)+(own?' (ты)':'')):'Грузоместо свободно';
 E('claimBtn').classList.toggle('hidden',c.status==='done'||!!c.employee_id);
 E('workCard').classList.toggle('hidden',c.status!=='started'||!own);
 E('finishCard').classList.toggle('hidden',c.status!=='started'||!own||!c.ready_for_qr);
 E('finishHint').textContent=c.approved_shortage?'Руководитель согласовал недостачу '+c.approved_shortage+' шт. Подтверди ШК грузоместа.':'Все позиции собраны. Подтверди ШК грузоместа.';
 E('shortageCard').classList.toggle('hidden',c.status!=='started'||!own);
 const rank=new Map(recommendedIds.map((id,idx)=>[id,idx]));
 const display=[...c.items].sort((a,b)=>{
  const done=Number(a.remaining===0)-Number(b.remaining===0);
  if(done)return done;
  return pickGuideReady?(rank.get(a.id)??99999)-(rank.get(b.id)??99999):0;
 });
 // Keep partially picked item active. Completed positions advance automatically.
 const active=c.items.find(i=>i.id===activeItemId&&i.remaining>0);
 if(!active)activeItemId=display.find(i=>i.remaining>0&&!i.shortage?.status?.includes('pending')&&i.shortage?.status!=='approved')?.id||
   display.find(i=>i.remaining>0)?.id||null;
 E('positionsCount').textContent=c.items.filter(i=>i.remaining>0).length+' осталось';
 E('items').innerHTML=display.map(i=>{
  const hint=pickGuide[i.id]||{};
  const loc=hint.location|| (hint.location_state==='multiple'?'Несколько ячеек':'Ячейка не указана');
  return '<button type="button" class="position-choice'+(i.remaining===0?' done':'')+
   (i.id===activeItemId?' current':'')+'" data-item-id="'+esc(i.id)+'"><b>'+
   esc(i.article)+' — <span>'+esc(loc)+'</span></b><small>Осталось '+i.remaining+' из '+i.qty+' шт.</small></button>';
 }).join('');
 updateActive(c);
 const canReport=c.items.filter(i=>i.remaining>0&&(!i.shortage||i.shortage.status==='rejected'));
 const oldItem=E('shortageItem').value;
 E('shortageItem').innerHTML=canReport.map(i=>'<option value="'+esc(i.id)+'">'+esc(i.article)+' · недостаёт '+i.remaining+' шт.</option>').join('');
 if(canReport.some(i=>i.id===oldItem))E('shortageItem').value=oldItem;
 E('shortageBtn').disabled=!canReport.length;
 E('shortageStatus').textContent=c.pending_shortages?'Ожидают согласования: '+c.pending_shortages+' поз.':c.approved_shortage?'Согласовано: '+c.approved_shortage+' шт.':'';
 E('undoBtn').disabled=!c.picked||!own||c.status!=='started';
 if(c.status==='done')status('Грузоместо собрано и подтверждено. Можно переходить к следующему.',false);
 else if(E('message').textContent==='Загрузка задания…'||E('message').textContent==='Готовлю сборку…')
  status('Задание загружено. Сканируй товар из списка.');
 if(verifiedItem){
  const match=c.items.find(i=>i.barcode===verifiedItem.barcode);
  if(!match||!match.remaining){verifiedItem=null;E('selected').textContent='Сканируй следующий товар.';E('barcode').value='';}
  else{verifiedItem=match;E('selected').textContent=match.article+' · осталось '+match.remaining+' шт.';E('quantity').max=match.remaining}
 }
}
function updateActive(c){
 const item=c.items.find(i=>i.id===activeItemId);
 if(!item){
  E('activeLocation').textContent='Все позиции обработаны';
  E('activeArticle').textContent=c.ready_for_qr?'Отсканируй ШК грузоместа':'Ожидается решение по недостачам';
  E('activeQuantity').textContent='';
  E('positionStep').textContent='';
  return;
 }
 const hint=pickGuide[item.id]||{};
 E('activeLocation').textContent=hint.location||
  (hint.location_state==='multiple'?'Несколько ячеек':'Ячейка не указана');
 E('activeArticle').textContent=item.article+' — '+(hint.location||'адрес не указан');
 E('activeQuantity').textContent='Осталось '+item.remaining+' из '+item.qty+' шт.';
 E('positionStep').textContent='Одна позиция';
}
E('items').addEventListener('click',e=>{
 const button=e.target.closest('[data-item-id]');
 if(!button||!cargo)return;
 const item=cargo.items.find(i=>i.id===button.dataset.itemId);
 if(!item||!item.remaining)return;
 activeItemId=item.id;
 draw(cargo);
 E('otherPositions').open=false;
});
async function refresh(){
 const d=await req('/cargo/'+encodeURIComponent(cargoId));await draw(d.cargo);
}
async function loadPickGuide(){
 try{
  const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/guide');
  pickGuide=d.guide||{};recommendedIds=d.recommended_ids||[];
  pickGuideReady=true;
  E('guideState').textContent=d.dimensions_available?
    'Рекомендуемый порядок по объёму. Можно сканировать любой товар.':
    'Размеры временно недоступны. Можно сканировать любой товар.';
 }catch(e){
  E('guideState').textContent='Подсказки недоступны, сканирование работает как обычно.';
 }
 if(cargo)await draw(cargo);
}


function verifyBarcode(){
 if(!cargo||!employee)return;
 const barcode=code(E('barcode').value),item=cargo.items.find(i=>i.barcode===barcode);
 if(!item){verifiedItem=null;status('Неверный товар: этого штрихкода нет в задании.',true);vibrate(false);E('barcode').value='';E('barcode').focus();return}
 if(!item.remaining){verifiedItem=null;status('Этот артикул уже собран полностью.',true);vibrate(false);E('barcode').value='';E('barcode').focus();return}
 if(item.shortage&&['pending','approved'].includes(item.shortage.status)){verifiedItem=null;status('По позиции есть недостача. Требуется решение руководителя.',true);E('barcode').value='';return}
 // A new SKU always starts at 1. Do not carry over quantity from a previous SKU.
 if(!verifiedItem||verifiedItem.barcode!==item.barcode)E('quantity').value=1;
 activeItemId=item.id;updateActive(cargo);
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
function pendingShortage(){try{return JSON.parse(localStorage.getItem(pendingShortageKey)||'null')}catch(e){return null}}
async function recoverShortage(){
 const p=pendingShortage();if(!p)return;
 if(p.employee_id!==employee.id){localStorage.removeItem(pendingShortageKey);return}
 if(p.cargo_id!==cargoId){location.replace('/ozon-fbo/pick?cargo_id='+encodeURIComponent(p.cargo_id));return}
 if(busy)return;
 busy=true;E('shortageBtn').disabled=true;
 try{
  const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/shortage','POST',p);
  localStorage.removeItem(pendingShortageKey);await draw(d.cargo);
  status('Недостача передана руководителю. Ожидай согласования.');
 }catch(e){
  if(e.status&&e.status<500){localStorage.removeItem(pendingShortageKey);status(e.message,true)}
  else status('Неизвестен результат отправки. Запрос сохранён для безопасного повтора.',true);
 }finally{busy=false;E('shortageBtn').disabled=false}
}
E('shortageBtn').addEventListener('click',async()=>{
 if(pendingShortage()){await recoverShortage();return}
 if(busy||!cargo||cargo.status!=='started'||cargo.employee_id!==employee.id)return;
 if(getPending()||getPendingUndo())return status('Сначала сверь ожидающее подтверждение скана.',true);
 const item=cargo.items.find(i=>i.id===E('shortageItem').value);
 if(!item||!item.remaining||(item.shortage&&item.shortage.status!=='rejected'))return status('Выбери позицию с неподтверждённой недостачей.',true);
 const reason=E('shortageReason').value;
 if(!confirm('Зафиксировать отсутствие '+item.remaining+' шт. товара '+item.article+'? План Ozon не изменится.'))return;
 const p={cargo_id:cargoId,employee_id:employee.id,item_id:item.id,reason,request_id:makeId()};
 localStorage.setItem(pendingShortageKey,JSON.stringify(p));await recoverShortage();
});
E('cargoCode').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();E('finishBtn').click()}});
E('finishBtn').addEventListener('click',async()=>{
 if(busy||getPending())return status('Сначала восстанови последнее подтверждение.',true);
 const scanned=code(E('cargoCode').value);if(!scanned)return status('Сканируй ШК грузоместа.',true);
 if(!confirm('Собрано '+cargo.picked+' шт., согласованная недостача '+cargo.approved_shortage+' шт. Завершить после проверки ШК грузоместа?'))return;
 busy=true;E('finishBtn').disabled=true;
 try{const d=await req('/cargo/'+encodeURIComponent(cargoId)+'/complete','POST',{cargo_barcode:scanned});await draw(d.cargo)}
 catch(e){status(e.message,true)}finally{busy=false;E('finishBtn').disabled=false}
});
async function startup(){
 const response=await fetch('/api/auth/me',{cache:'no-store'});
 if(!response.ok){location.href='/login';return}
 const d=await response.json();employee=d.employee;E('identity').textContent='Сотрудник: '+employee.name;
 for(const key of [pendingKey,pendingUndoKey,pendingShortageKey]){
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
 // Optional read-only hints load in parallel and never delay scan recovery.
 void loadPickGuide();
 await recoverPending();
 await recoverUndo();
 await recoverShortage();
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
function statusLabel(c){const approved=(c.shortages||[]).filter(x=>x.status==='approved').reduce((n,x)=>n+x.missing,0);return c.status==='done'&&approved?'Готово с недостачей':({planned:'План',started:'Собирается',done:'Собрано'}[c.status]||c.status)}
async function load(){
 const r=await fetch('/api/manager/ozon-fbo/progress',{cache:'no-store'}),d=await r.json();
 if(!r.ok)throw Error(d.error||'Не удалось загрузить');
 list=d.cargo||[];
 document.getElementById('rows').innerHTML=list.length?list.map(c=>
 '<tr class="'+progressState(c)+'"><td><b>'+esc(c.supply)+'</b><div class="small">'+esc(c.destination)+' · '+esc(c.label)+'</div></td>'+
 '<td>'+esc(c.barcode)+'</td><td>'+esc(c.employee||'—')+'</td>'+
 '<td>'+esc(c.picked)+' / '+esc(c.qty)+'<div class="progress '+progressState(c)+'"><div style="width:'+Math.min(100,c.qty?100*c.picked/c.qty:0)+'%"></div></div><div class="small">'+esc((c.shortages||[]).filter(x=>x.status==='approved').reduce((n,x)=>n+x.missing,0)?'Согласованная недостача: '+(c.shortages||[]).filter(x=>x.status==='approved').reduce((n,x)=>n+x.missing,0)+' шт.':(c.shortages||[]).some(x=>x.status==='pending')?'Ожидается решение по недостаче':'')+'</div></td>'+
 '<td><span class="status-badge '+progressState(c)+'">'+(c.status==='done'?'✓ ':'')+esc(statusLabel(c))+'</span></td>'+
 '<td><button data-history="'+esc(c.id)+'" class="secondary">История</button> '+
 (c.status!=='done'&&c.employee_id?'<button data-release="'+esc(c.id)+'">Передать</button>':'')+
 (c.shortages||[]).filter(x=>x.status==='pending').map(x=>'<div style="margin-top:8px;font-size:12px">'+esc(x.article)+' · '+esc(x.missing)+' шт.<div>'+esc(x.reason)+'</div><button data-shortage-cargo="'+esc(c.id)+'" data-shortage-item="'+esc(x.item_id)+'" data-decision="approve">Согласовать</button> <button class="secondary" data-shortage-cargo="'+esc(c.id)+'" data-shortage-item="'+esc(x.item_id)+'" data-decision="reject">Отклонить</button></div>').join('')+
 '</td></tr>').join(''):
 '<tr><td colspan="6">Нет поставок.</td></tr>';
}
document.getElementById('rows').addEventListener('click',async e=>{
 const history=e.target.closest('button[data-history]'),release=e.target.closest('button[data-release]');
 const shortage=e.target.closest('button[data-shortage-item]');
 if(shortage){
  const cargo=list.find(c=>c.id===shortage.dataset.shortageCargo);
  const item=(cargo?.shortages||[]).find(x=>x.item_id===shortage.dataset.shortageItem);
  if(!cargo||!item||item.status!=='pending')return alert('Недостача уже обработана. Обнови список.');
  const approve=shortage.dataset.decision==='approve';
  const reason=prompt((approve?'Результат проверки остатков: ':'Почему недостача отклонена: ')+item.article+' · '+item.missing+' шт.','Проверено руководителем');
  if(reason===null)return;
  if(reason.trim().length<8)return alert('Причина должна содержать не менее 8 символов.');
  if(!confirm((approve?'Согласовать':'Отклонить')+' недостачу '+item.missing+' шт.? План Ozon не меняется автоматически.'))return;
  shortage.disabled=true;
  try{
   const r=await fetch('/api/manager/ozon-fbo/shortage/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
     cargo_id:cargo.id,item_id:item.item_id,decision:approve?'approve':'reject',reason:reason.trim()})});
   const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка согласования');
   await load();
  }catch(err){alert(err.message)}finally{shortage.disabled=false}
  return;
 }
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
