import os
import time
import threading
import requests
from flask import Flask, request, jsonify, session, redirect
from wms import wms_bp, init_wms_db, get_session_employee, log_event, query_one, execute, now_iso

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "mini-wms-warehouse-2026")
app.register_blueprint(wms_bp)

@app.before_request
def require_employee_login():
    path = request.path
    public_paths = {"/login", "/api/auth/login", "/api/health", "/supervisor-terminal", "/api/supervisor-terminal/qr"}
    if path in public_paths or path.startswith("/static/"):
        return None
    employee = get_session_employee()
    if employee:
        return None
    if path.startswith("/api/"):
        return jsonify(error="Требуется вход по бейджу"), 401
    return redirect("/login")


API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()

# Быстрый индекс палетных штрихкодов в памяти Render.
# После первого прогрева скан не перебирает каталог МойСклад заново.
PACK_CACHE = {}
PRODUCT_BARCODE_CACHE = {}
PRODUCT_LIST_CACHE = {"ts": 0.0, "rows": []}
DETAILED_PRODUCT_IDS = set()
CACHE_LOCK = threading.RLock()
WARMUP_STATE = {"running": False, "ready": False, "indexed": 0, "error": ""}
PRODUCT_LIST_TTL = 3600

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111111">
<title>Оприходование палет</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f5f6f8;color:#111}
.wrap{max-width:560px;margin:0 auto;padding:16px}
.card{background:#fff;border-radius:18px;padding:18px;box-shadow:0 2px 12px rgba(0,0,0,.08);margin-bottom:14px}
h1{font-size:26px;margin:0 0 8px}
h2{font-size:19px;margin:0 0 10px}
.muted{color:#666;font-size:14px;line-height:1.35}
label{display:block;margin:12px 0 6px;font-size:13px;color:#666}
input,select{width:100%;padding:14px;font-size:16px;border:2px solid #d0d4d9;border-radius:12px;background:#fff}
button{width:100%;padding:15px;font-size:17px;font-weight:700;border:0;border-radius:12px;cursor:pointer;background:#111;color:#fff;margin-top:10px}
button.secondary{background:#e9ebee;color:#111}
button.danger{background:#b42318}
button:disabled{opacity:.45;cursor:not-allowed}
#reader{width:100%;margin-top:14px;overflow:hidden;border-radius:14px}
.hidden{display:none!important}
.ok,.err,.info{margin-top:12px;padding:13px;border-radius:12px}
.ok{background:#eaf8ee}
.err{background:#fdecec}
.info{background:#eef3ff}
.stats{margin-top:14px;padding:14px;background:#f0f2f5;border-radius:12px}
.big{font-size:28px;font-weight:800}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.small{font-size:12px;color:#777}
.activeDoc{padding:13px;background:#f0f2f5;border-radius:12px;margin-top:10px}
.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.nav a{display:flex;align-items:center;justify-content:center;text-align:center;min-height:44px;padding:9px 8px;border-radius:12px;text-decoration:none;font-weight:700;font-size:14px;background:#e7edf2;color:#314657;border:1px solid #d2dde5}
.nav a.active{background:#48647a;color:#fff;border-color:#48647a}
</style>
</head>
<body>
<div class="wrap">
<div class="nav">
  <a class="active" href="/">📦 Приёмка</a>
  <a href="/fbs">🚀 FBS</a>
  <a id="statsLink" class="hidden" href="/stats">📊 Статистика</a>
</div>
<div class="small" style="margin:0 2px 10px"><span id="navIdentity"></span> · <a href="/exit">🚪 Завершить смену</a></div>

<div class="card">
  <h1>Склад → Оприходование</h1>
  <div class="muted">Создай Оприходование: выбери склад и поставщика через статус, затем сканируй палеты. После работы проведи документ.</div>
</div>

<div class="card" id="docCard">
  <h2>Документ Оприходования</h2>

  <div id="activeDoc" class="activeDoc hidden"></div>

  <button id="newDocBtn" onclick="showNewDoc()">＋ Новое оприходование</button>
  <button class="secondary" onclick="showExisting()">Открыть существующее</button>

  <div id="newDocForm" class="hidden">
    <label>Склад</label>
    <select id="storeSelect"><option value="">Загрузка…</option></select>

    <label>Поставщик (статус Оприходования)</label>
    <select id="stateSelect"><option value="">Загрузка…</option></select>

    <label>Организация</label>
    <select id="orgSelect"><option value="">Загрузка…</option></select>

    <button onclick="createReceipt()">Создать оприходование</button>
    <button class="secondary" onclick="hideDocForms()">Отмена</button>
  </div>

  <div id="existingForm" class="hidden">
    <label>Последние оприходования</label>
    <select id="receiptSelect"><option value="">Загрузка…</option></select>
    <button onclick="useSelectedReceipt()">Открыть выбранное</button>
    <button class="secondary" onclick="loadReceipts()">↻ Обновить список</button>
    <button class="secondary" onclick="hideDocForms()">Отмена</button>
  </div>
</div>

<div class="card" id="scanCard">
  <h2>Сканирование палет</h2>
  <div class="muted">Один скан палетного ШК добавляет количество из упаковки товара в активное Оприходование.</div>

  <button id="cameraBtn" onclick="toggleCamera('full')" disabled>📷 Сканировать полную палету</button>
  <button class="secondary" id="partialBtn" onclick="toggleCamera('partial')" disabled>📦 Неполная палета</button>
  <button class="secondary" id="oszBtn" onclick="startOszFlow()" disabled>🏭 Палета ОСЗ / SSCC</button>
  <button class="secondary" id="manualBtn" onclick="toggleManual()" disabled>⌨️ Ручной ввод / ТСД</button>

  <div id="reader" class="hidden"></div>

  <div id="oszForm" class="info hidden">
    <b>Палета ОСЗ</b>
    <div class="small" id="oszStatus">Сначала отсканируйте SSCC палеты.</div>
    <div id="oszProductInfo" class="hidden"></div>
    <label id="oszQtyLabel" class="hidden">Количество по этикетке ОСЗ, шт.</label>
    <input id="oszQty" class="hidden" inputmode="numeric" type="number" min="1" step="1" placeholder="Например 440">
    <button id="oszProductBtn" class="secondary hidden" onclick="scanOszProduct()">Сканировать ШК товара</button>
    <button id="oszAcceptBtn" class="hidden" onclick="acceptOszPallet()">Принять палету ОСЗ</button>
    <button class="secondary" onclick="cancelOszFlow()">Отмена</button>
  </div>

  <div id="manual" class="hidden">
    <label>Штрихкод палеты</label>
    <input id="barcode" inputmode="numeric" autocomplete="off" placeholder="Например 2000000000404">
    <button onclick="sendManualFull()">Добавить как полную палету</button>
    <button class="secondary" onclick="sendManualPartial()">Неполная палета вручную</button>
  </div>

  <div id="partialForm" class="info hidden">
    <b id="partialTitle">Неполная палета</b>
    <div class="small" id="partialMeta"></div>
    <label>Фактически приехало, шт.</label>
    <input id="partialQty" inputmode="numeric" type="number" min="1" step="1" placeholder="Например 100">
    <button onclick="confirmPartial()">Принять фактическое количество</button>
    <button class="secondary" onclick="cancelPartial()">Отмена</button>
  </div>

  <div id="result"></div>

  <div class="stats">
    <div class="row">
      <div><div class="small">Палет принято</div><div class="big" id="pallets">0</div></div>
      <div><div class="small">Единиц товара</div><div class="big" id="units">0</div></div>
    </div>
  </div>

  <button id="completeBtn" class="danger" onclick="completeReceipt()" disabled>✓ Провести оприходование</button>
</div>

<div class="card hidden" id="completionCard">
  <h1>✅ Приёмка завершена</h1>
  <div class="muted">Документ проведён. Приёмка закрыта, товар записан в МойСклад.</div>
  <div id="completionSummary" class="stats"></div>
  <button onclick="startAnotherReceipt()">＋ Новая приёмка</button>
  <button class="secondary" onclick="location.href='/'">На главный экран</button>
</div>

</div>

<script>
let scanner=null;
let pallets=0;
let units=0;
let busy=false;
let activeReceiptId='';
let activeReceiptName='';
let audioCtx=null;
let scanMode='full';
let partialBarcode='';
let partialStandardQty=0;
let lastBarcode='';
let lastScanAt=0;
let oszSscc='';
let oszProductBarcode='';
let oszProductMeta=null;

function initAudio(){
  try{
    if(!audioCtx){
      const Ctx=window.AudioContext||window.webkitAudioContext;
      if(Ctx)audioCtx=new Ctx();
    }
    if(audioCtx && audioCtx.state==='suspended')audioCtx.resume();
  }catch(e){}
}

function playTone(freq=880,duration=0.12,type='sine'){
  try{
    initAudio();
    if(!audioCtx)return;
    const osc=audioCtx.createOscillator();
    const gain=audioCtx.createGain();
    osc.type=type;
    osc.frequency.value=freq;
    gain.gain.setValueAtTime(0.0001,audioCtx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.18,audioCtx.currentTime+0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001,audioCtx.currentTime+duration);
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start();
    osc.stop(audioCtx.currentTime+duration+0.02);
  }catch(e){}
}

function successFeedback(){
  playTone(980,0.10,'sine');
  setTimeout(()=>playTone(1320,0.12,'sine'),110);
  try{if(navigator.vibrate)navigator.vibrate([80,40,80]);}catch(e){}
}

function errorFeedback(){
  playTone(220,0.22,'square');
  try{if(navigator.vibrate)navigator.vibrate([180,80,180]);}catch(e){}
}

const stateSelect=document.getElementById('stateSelect');
const orgSelect=document.getElementById('orgSelect');
const storeSelect=document.getElementById('storeSelect');
const receiptSelect=document.getElementById('receiptSelect');

function setActiveReceipt(id,name,meta=''){
  activeReceiptId=id||'';
  activeReceiptName=name||'';
  localStorage.setItem('receipt_id',activeReceiptId);
  localStorage.setItem('receipt_name',activeReceiptName);

  const box=document.getElementById('activeDoc');
  const enabled=!!activeReceiptId;
  document.getElementById('cameraBtn').disabled=!enabled;
  document.getElementById('partialBtn').disabled=!enabled;
  document.getElementById('oszBtn').disabled=!enabled;
  document.getElementById('manualBtn').disabled=!enabled;
  document.getElementById('completeBtn').disabled=!enabled;

  if(enabled){
    box.classList.remove('hidden');
    box.innerHTML='<b>'+escapeHtml(activeReceiptName)+'</b><br><span class="small">'+escapeHtml(meta||'Активная Приёмка')+'</span>';
  }else{
    box.classList.add('hidden');
    box.innerHTML='';
  }
}

function showNewDoc(){
  document.getElementById('newDocForm').classList.remove('hidden');
  document.getElementById('existingForm').classList.add('hidden');
}
function showExisting(){
  document.getElementById('existingForm').classList.remove('hidden');
  document.getElementById('newDocForm').classList.add('hidden');
  loadReceipts();
}
function hideDocForms(){
  document.getElementById('newDocForm').classList.add('hidden');
  document.getElementById('existingForm').classList.add('hidden');
}

async function loadBootstrap(){
  try{
    const r=await fetch('/api/bootstrap');
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось загрузить справочники');

    fillSelect(storeSelect,data.stores,'Выбери склад');
    fillSelect(stateSelect,data.states,'Выбери поставщика / статус');
    fillSelect(orgSelect,data.organizations,'Выбери организацию');

    if(data.organizations.length===1)orgSelect.value=data.organizations[0].id;
    if(data.stores.length===1)storeSelect.value=data.stores[0].id;
  }catch(e){
    showError(e.message);
  }
}

function fillSelect(el,items,placeholder){
  el.innerHTML='<option value="">'+placeholder+'</option>';
  items.forEach(x=>{
    const o=document.createElement('option');
    o.value=x.id;
    o.textContent=x.name;
    el.appendChild(o);
  });
}

async function createReceipt(){
  const stateId=stateSelect.value;
  const organizationId=orgSelect.value;
  const storeId=storeSelect.value;

  if(!stateId||!organizationId||!storeId){
    showError('Выбери склад, поставщика/статус и организацию.');
    return;
  }

  try{
    const r=await fetch('/api/receipts',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        state_id:stateId,
        organization_id:organizationId,
        store_id:storeId
      })
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось создать Оприходование');

    pallets=0; units=0;
    document.getElementById('pallets').textContent='0';
    document.getElementById('units').textContent='0';

    setActiveReceipt(data.id,data.name,data.state+' · '+data.store+' · черновик');
    hideDocForms();
    document.getElementById('result').innerHTML='<div class="ok">Оприходование создано. Можно сканировать палеты.</div>';
  }catch(e){
    showError(e.message);
  }
}

async function loadReceipts(){
  receiptSelect.innerHTML='<option value="">Загружаю…</option>';
  try{
    const r=await fetch('/api/receipts');
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось загрузить Приёмки');

    receiptSelect.innerHTML='<option value="">Выбери Приёмку</option>';
    data.receipts.forEach(x=>{
      const o=document.createElement('option');
      o.value=x.id;
      o.textContent=x.label;
      o.dataset.name=x.name;
      o.dataset.meta=x.meta;
      receiptSelect.appendChild(o);
    });
  }catch(e){
    receiptSelect.innerHTML='<option value="">Ошибка загрузки</option>';
    showError(e.message);
  }
}

function useSelectedReceipt(){
  const o=receiptSelect.options[receiptSelect.selectedIndex];
  if(!o||!o.value){
    showError('Выбери Приёмку.');
    return;
  }
  setActiveReceipt(o.value,o.dataset.name||o.textContent,o.dataset.meta||'');
  hideDocForms();
}

async function completeReceipt(){
  if(!activeReceiptId)return;
  if(!confirm('Провести это Оприходование? После этого документ будет проведён в МойСклад.'))return;

  try{
    if(scanner)await stopScanner();

    const fallbackPallets=pallets;
    const fallbackUnits=units;
    const r=await fetch('/api/receipts/'+activeReceiptId+'/complete',{method:'POST'});
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось провести Оприходование');

    setActiveReceipt('','','');

    document.getElementById('docCard').classList.add('hidden');
    document.getElementById('scanCard').classList.add('hidden');
    document.getElementById('completionCard').classList.remove('hidden');

    const acceptedPallets=Number(data.pallets||fallbackPallets||0);
    const acceptedUnits=Number(data.total_units||fallbackUnits||0);
    const positions=Number(data.positions||0);
    const employee=data.employee||document.getElementById('navIdentity').textContent||'';

    document.getElementById('completionSummary').innerHTML=
      '<div style="margin-bottom:10px"><b>'+escapeHtml(data.name||'Оприходование')+'</b></div>'+
      '<div class="row">'+
        '<div><div class="small">Палет принято</div><div class="big">'+acceptedPallets+'</div></div>'+
        '<div><div class="small">Единиц товара</div><div class="big">'+acceptedUnits+'</div></div>'+
      '</div>'+
      '<div style="margin-top:12px"><b>Позиций:</b> '+positions+'</div>'+
      (employee?'<div style="margin-top:6px"><b>Сотрудник:</b> '+escapeHtml(employee)+'</div>':'')+
      '<div style="margin-top:6px"><b>Статус:</b> Проведено</div>';
  }catch(e){
    showError(e.message);
  }
}

function startAnotherReceipt(){
  pallets=0;
  units=0;
  document.getElementById('pallets').textContent='0';
  document.getElementById('units').textContent='0';
  document.getElementById('result').innerHTML='';
  document.getElementById('completionCard').classList.add('hidden');
  document.getElementById('docCard').classList.remove('hidden');
  document.getElementById('scanCard').classList.remove('hidden');
  showNewDoc();
}

function toggleManual(){
  initAudio();
  document.getElementById('manual').classList.toggle('hidden');
  setTimeout(()=>document.getElementById('barcode').focus(),100);
}

async function stopScanner(){
  if(scanner){
    try{await scanner.stop();}catch(e){}
    scanner=null;
  }
  document.getElementById('reader').classList.add('hidden');
  document.getElementById('cameraBtn').textContent='📷 Сканировать полную палету';
  document.getElementById('partialBtn').textContent='📦 Неполная палета';
  document.getElementById('oszBtn').textContent='🏭 Палета ОСЗ / SSCC';
}

async function toggleCamera(mode='full'){
  initAudio();
  if(!activeReceiptId){showError('Сначала создай или открой Оприходование.');return;}

  if(scanner){
    await stopScanner();
    return;
  }

  scanMode=mode;

  if(typeof Html5Qrcode==='undefined'){
    showError('Модуль сканирования не загрузился. Обнови страницу.');
    return;
  }

  document.getElementById('reader').classList.remove('hidden');
  scanner=new Html5Qrcode("reader");

  try{
    await scanner.start(
      {facingMode:"environment"},
      {fps:15,qrbox:{width:320,height:160},formatsToSupport:[
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.CODE_128,
        Html5QrcodeSupportedFormats.QR_CODE
      ]},
      async(decodedText)=>{
        if(busy)return;
        const now=Date.now();
        if(decodedText===lastBarcode && now-lastScanAt<3000)return;
        lastBarcode=decodedText;
        lastScanAt=now;
        if(scanMode==='partial') await preparePartial(decodedText);
        else if(scanMode==='osz_sscc') await handleOszSscc(decodedText);
        else if(scanMode==='osz_product') await prepareOszProduct(decodedText);
        else await sendScan(decodedText);
      },
      ()=>{}
    );
    if(scanMode==='partial') document.getElementById('partialBtn').textContent='⏹ Остановить камеру';
    else if(scanMode==='osz_sscc'||scanMode==='osz_product') document.getElementById('oszBtn').textContent='⏹ Остановить камеру';
    else document.getElementById('cameraBtn').textContent='⏹ Остановить камеру';
  }catch(e){
    await stopScanner();
    showError('Не удалось открыть камеру: '+e);
  }
}

async function sendManualFull(){
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;
  await sendScan(code);
  input.value='';
  input.focus();
}

async function sendManualPartial(){
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;
  await preparePartial(code);
  input.value='';
}

async function preparePartial(barcode){
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  busy=true;
  try{
    const r=await fetch('/api/pack-info',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось определить упаковку');

    await stopScanner();

    partialBarcode=barcode;
    partialStandardQty=Number(data.standard_qty||0);
    document.getElementById('partialTitle').textContent=(data.article||data.name||'Неполная палета');
    document.getElementById('partialMeta').textContent='Стандартная палета: '+partialStandardQty+' шт. · '+barcode;
    document.getElementById('partialQty').value='';
    document.getElementById('partialQty').max=String(partialStandardQty);
    document.getElementById('partialForm').classList.remove('hidden');
    setTimeout(()=>document.getElementById('partialQty').focus(),100);
    successFeedback();
  }catch(e){
    showError(e.message);
  }finally{
    busy=false;
  }
}

function cancelPartial(){
  partialBarcode='';
  partialStandardQty=0;
  document.getElementById('partialForm').classList.add('hidden');
}

async function confirmPartial(){
  const qty=Number(document.getElementById('partialQty').value);
  if(!partialBarcode){showError('Сначала отсканируй палету.');return;}
  if(!Number.isFinite(qty)||qty<=0){showError('Укажи фактическое количество больше нуля.');return;}
  if(qty>partialStandardQty){showError('Фактическое количество не может быть больше стандартной палеты '+partialStandardQty+' шт.');return;}

  const barcode=partialBarcode;
  cancelPartial();
  await sendScan(barcode,qty);
}

function resetOszForm(){
  oszSscc='';
  oszProductBarcode='';
  oszProductMeta=null;
  document.getElementById('oszStatus').textContent='Сначала отсканируйте SSCC палеты.';
  document.getElementById('oszProductInfo').classList.add('hidden');
  document.getElementById('oszProductInfo').innerHTML='';
  document.getElementById('oszQtyLabel').classList.add('hidden');
  document.getElementById('oszQty').classList.add('hidden');
  document.getElementById('oszQty').value='';
  document.getElementById('oszProductBtn').classList.add('hidden');
  document.getElementById('oszAcceptBtn').classList.add('hidden');
}

async function startOszFlow(){
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  resetOszForm();
  document.getElementById('oszForm').classList.remove('hidden');
  await toggleCamera('osz_sscc');
}

async function handleOszSscc(raw){
  busy=true;
  try{
    const r=await fetch('/api/osz/check',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sscc:raw})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось проверить SSCC');

    await stopScanner();
    oszSscc=data.sscc;
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>Теперь отсканируйте ШК товара на палете.';
    document.getElementById('oszProductBtn').classList.remove('hidden');
    successFeedback();
  }catch(e){
    await stopScanner();
    showError(e.message);
  }finally{
    busy=false;
  }
}

async function scanOszProduct(){
  if(!oszSscc){showError('Сначала отсканируйте SSCC палеты ОСЗ.');return;}
  await toggleCamera('osz_product');
}

async function prepareOszProduct(barcode){
  busy=true;
  try{
    const r=await fetch('/api/osz/product-info',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Товар не найден');

    await stopScanner();
    oszProductBarcode=barcode;
    oszProductMeta=data;
    document.getElementById('oszProductInfo').innerHTML=
      '<div style="margin-top:10px"><b>'+escapeHtml(data.article||data.name||'Товар')+'</b><br>'+
      '<span class="small">'+escapeHtml(data.name||'')+
      (data.standard_qty?'<br>Квант МойСклад: '+data.standard_qty+' шт.':'')+
      '</span></div>';
    document.getElementById('oszProductInfo').classList.remove('hidden');
    document.getElementById('oszQtyLabel').classList.remove('hidden');
    document.getElementById('oszQty').classList.remove('hidden');
    document.getElementById('oszQty').value='';
    document.getElementById('oszAcceptBtn').classList.remove('hidden');
    document.getElementById('oszProductBtn').classList.add('hidden');
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>Проверьте количество по этикетке ОСЗ.';
    setTimeout(()=>document.getElementById('oszQty').focus(),100);
    successFeedback();
  }catch(e){
    await stopScanner();
    showError(e.message);
  }finally{
    busy=false;
  }
}

async function acceptOszPallet(){
  if(!oszSscc||!oszProductBarcode){showError('Сначала отсканируйте SSCC и товар.');return;}
  const qty=Number(document.getElementById('oszQty').value);
  if(!Number.isFinite(qty)||qty<=0){showError('Введите количество с этикетки ОСЗ.');return;}

  busy=true;
  try{
    const r=await fetch('/api/osz/accept',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        receipt_id:activeReceiptId,
        sscc:oszSscc,
        product_barcode:oszProductBarcode,
        quantity:qty
      })
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось принять палету ОСЗ');

    pallets+=1;
    units+=Number(data.added||0);
    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;
    successFeedback();
    document.getElementById('result').innerHTML=
      '<div class="ok"><b>Палета ОСЗ принята</b><br>'+
      escapeHtml(data.article||data.name||'')+' · <b>'+data.added+' шт.</b><br>'+
      '<span class="small">SSCC '+escapeHtml(data.sscc)+'</span></div>';

    resetOszForm();
    document.getElementById('oszForm').classList.add('hidden');
  }catch(e){
    showError(e.message);
  }finally{
    busy=false;
  }
}

async function cancelOszFlow(){
  if(scanner)await stopScanner();
  resetOszForm();
  document.getElementById('oszForm').classList.add('hidden');
}

async function sendScan(barcode,quantity=null){
  if(busy)return;
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  busy=true;

  try{
    const r=await fetch('/api/scan',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode,receipt_id:activeReceiptId,quantity:quantity})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Ошибка');

    pallets+=1;
    units+=Number(data.added||0);
    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;

    successFeedback();

    // После успешного сканирования камерой сразу закрываем её.
    // Это исключает случайный повторный захват той же палеты.
    if(scanner){
      await stopScanner();
    }

    document.getElementById('result').innerHTML=
      '<div class="ok"><b>'+escapeHtml(data.article||data.name||'')+'</b><br>'+
      'Добавлено: <b>'+data.added+' шт.</b><br>'+
      'Теперь в Приёмке: <b>'+data.total+' шт.</b><br>'+
      '<span class="small">'+escapeHtml(data.pack_name||'')+' · '+escapeHtml(data.barcode)+'</span></div>';
  }catch(e){
    showError(e.message);
  }finally{
    setTimeout(()=>{busy=false;},900);
  }
}

function showError(message){
  errorFeedback();
  document.getElementById('result').innerHTML='<div class="err">'+escapeHtml(String(message))+'</div>';
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
}

document.getElementById('barcode').addEventListener('keydown',e=>{
  if(e.key==='Enter')sendManualFull();
});

async function loadWmsIdentity(){
  try{
    const r=await fetch('/api/auth/me');
    if(!r.ok){location.href='/login';return;}
    const d=await r.json();
    document.getElementById('navIdentity').textContent=d.employee.name+(d.employee.badge_number?' · бейдж №'+d.employee.badge_number:'');
    if(d.employee.role==='manager')document.getElementById('statsLink').classList.remove('hidden');
    await fetch('/api/operation/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'receiving'})});
  }catch(e){}
}
loadWmsIdentity();
loadBootstrap();
fetch('/api/warmup',{method:'POST'}).catch(()=>{});
const savedId=localStorage.getItem('receipt_id')||'';
const savedName=localStorage.getItem('receipt_name')||'';
if(savedId)setActiveReceipt(savedId,savedName||'Ранее выбранная Приёмка','Сохранено на этом телефоне');
</script>
</body>
</html>"""

def headers():
    if not TOKEN:
        raise RuntimeError("MOYSKLAD_TOKEN не задан")
    return {
        "Authorization": f"Bearer {TOKEN}",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
    }

def meta(entity_type, entity_id):
    return {
        "meta": {
            "href": f"{API_BASE}/entity/{entity_type}/{entity_id}",
            "type": entity_type,
            "mediaType": "application/json",
        }
    }

def get_rows(entity, params=None):
    r = requests.get(
        f"{API_BASE}/entity/{entity}",
        headers=headers(),
        params=params or {"limit": 1000},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("rows", [])

def get_enter_states():
    r = requests.get(
        f"{API_BASE}/entity/enter/metadata",
        headers=headers(),
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("states", []) or []

def bootstrap_data():
    organizations = get_rows("organization", {"limit": 1000, "order": "name"})
    stores = get_rows("store", {"limit": 1000, "order": "name"})
    states = get_enter_states()

    return {
        "organizations": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in organizations if not x.get("archived")],
        "stores": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in stores if not x.get("archived")],
        "states": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in states],
    }

def list_receipts():
    rows = get_rows("enter", {
        "limit": 30,
        "order": "moment,desc",
        "expand": "state,store",
    })
    result = []
    for row in rows:
        moment = (row.get("moment") or "")[:16].replace("T", " ")
        name = row.get("name") or "Без номера"
        state = (row.get("state") or {}).get("name") or "без статуса"
        store = (row.get("store") or {}).get("name") or "без склада"
        status = "проведено" if row.get("applicable") else "черновик"
        result.append({
            "id": row.get("id"),
            "name": name,
            "label": f"{name} · {moment} · {store} · {state} · {status}",
            "meta": f"{store} · {state} · {status}",
        })
    return result

def create_receipt(state_id, organization_id, store_id):
    states = get_enter_states()
    state = next((x for x in states if x.get("id") == state_id), None)
    if not state or not state.get("meta"):
        raise ValueError("Выбранный статус Оприходования не найден.")

    payload = {
        "organization": meta("organization", organization_id),
        "store": meta("store", store_id),
        "state": {"meta": state["meta"]},
        "applicable": False,
        "description": "Оприходование создано мобильным сканером палет",
    }
    r = requests.post(
        f"{API_BASE}/entity/enter",
        headers=headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def complete_receipt(receipt_id):
    r = requests.put(
        f"{API_BASE}/entity/enter/{receipt_id}",
        headers=headers(),
        json={"applicable": True},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def _barcode_values(value):
    values = []
    if value is None:
        return values
    if isinstance(value, str):
        s = value.strip()
        if s:
            values.append(s)
        return values
    if isinstance(value, (int, float)):
        values.append(str(value).strip())
        return values
    if isinstance(value, list):
        for item in value:
            values.extend(_barcode_values(item))
        return values
    if isinstance(value, dict):
        for key, item in value.items():
            key_l = str(key).lower()
            if "barcode" in key_l or key_l in ("ean13", "ean8", "code128", "qr"):
                values.extend(_barcode_values(item))
            elif isinstance(item, (dict, list)):
                values.extend(_barcode_values(item))
        return values
    return values

def _pack_barcodes(pack):
    values = []
    values.extend(_barcode_values(pack.get("barcode")))
    values.extend(_barcode_values(pack.get("barcodes")))
    return {str(v).strip() for v in values if str(v).strip()}

def _index_product(product):
    added = 0
    with CACHE_LOCK:
        for barcode in _barcode_values(product.get("barcodes")):
            PRODUCT_BARCODE_CACHE[str(barcode).strip()] = product
        for pack in product.get("packs", []) or []:
            for barcode in _pack_barcodes(pack):
                PACK_CACHE[barcode] = {"product": product, "pack": pack}
                added += 1
    return added

def _get_product_list(force=False):
    now = time.time()
    with CACHE_LOCK:
        if (
            not force
            and PRODUCT_LIST_CACHE["rows"]
            and now - PRODUCT_LIST_CACHE["ts"] < PRODUCT_LIST_TTL
        ):
            return PRODUCT_LIST_CACHE["rows"]

    rows_all = []
    limit = 1000
    offset = 0
    while True:
        r = requests.get(
            f"{API_BASE}/entity/product",
            headers=headers(),
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("rows", [])
        rows_all.extend(rows)
        for product in rows:
            _index_product(product)
        if len(rows) < limit:
            break
        offset += limit

    with CACHE_LOCK:
        PRODUCT_LIST_CACHE["rows"] = rows_all
        PRODUCT_LIST_CACHE["ts"] = now
    return rows_all

def _load_full_product(product):
    product_id = product.get("id")
    if not product_id:
        return product

    with CACHE_LOCK:
        if product_id in DETAILED_PRODUCT_IDS:
            return product

    r = requests.get(
        f"{API_BASE}/entity/product/{product_id}",
        headers=headers(),
        timeout=30,
    )
    r.raise_for_status()
    full_product = r.json()
    _index_product(full_product)

    with CACHE_LOCK:
        DETAILED_PRODUCT_IDS.add(product_id)
        WARMUP_STATE["indexed"] = len(PACK_CACHE)
    return full_product

def find_product_pack_by_barcode(barcode):
    target = str(barcode).strip()

    # Горячий путь: обычно один словарный lookup, без запроса к МойСклад.
    with CACHE_LOCK:
        found = PACK_CACHE.get(target)
    if found:
        return found["product"], found["pack"]

    rows = _get_product_list()

    # После загрузки списка код мог уже попасть в индекс.
    with CACHE_LOCK:
        found = PACK_CACHE.get(target)
    if found:
        return found["product"], found["pack"]

    # Только для еще не просмотренных карточек идем за полными данными.
    for product in rows:
        product_id = product.get("id")
        with CACHE_LOCK:
            already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
        if already_detailed:
            continue

        full_product = _load_full_product(product)
        with CACHE_LOCK:
            found = PACK_CACHE.get(target)
        if found:
            return found["product"], found["pack"]

    return None, None

def find_product_by_any_barcode(barcode):
    target = str(barcode).strip()

    with CACHE_LOCK:
        pack_found = PACK_CACHE.get(target)
        product_found = PRODUCT_BARCODE_CACHE.get(target)
    if pack_found:
        return pack_found["product"], pack_found["pack"]
    if product_found:
        return product_found, {"name": "ШК товара", "quantity": 1}

    rows = _get_product_list()

    with CACHE_LOCK:
        pack_found = PACK_CACHE.get(target)
        product_found = PRODUCT_BARCODE_CACHE.get(target)
    if pack_found:
        return pack_found["product"], pack_found["pack"]
    if product_found:
        return product_found, {"name": "ШК товара", "quantity": 1}

    for product in rows:
        product_id = product.get("id")
        with CACHE_LOCK:
            already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
        if already_detailed:
            continue

        _load_full_product(product)
        with CACHE_LOCK:
            pack_found = PACK_CACHE.get(target)
            product_found = PRODUCT_BARCODE_CACHE.get(target)
        if pack_found:
            return pack_found["product"], pack_found["pack"]
        if product_found:
            return product_found, {"name": "ШК товара", "quantity": 1}

    return None, None


def normalize_sscc(value):
    raw = str(value or "").strip().replace(" ", "")
    if raw.startswith("]C1"):
        raw = raw[3:]
    if raw.startswith("(00)"):
        raw = raw[4:]
    elif raw.startswith("00") and len(raw) == 20 and raw.isdigit():
        raw = raw[2:]

    if len(raw) != 18 or not raw.isdigit():
        raise ValueError("Это не SSCC ОСЗ: ожидается GS1 код (00) + 18 цифр.")

    body = raw[:-1]
    supplied = int(raw[-1])
    total = 0
    for index, digit in enumerate(reversed(body)):
        total += int(digit) * (3 if index % 2 == 0 else 1)
    expected = (10 - (total % 10)) % 10
    if supplied != expected:
        raise ValueError("SSCC считан с ошибкой: не сходится контрольная цифра.")

    return raw


def _warm_pack_cache_worker():
    try:
        with CACHE_LOCK:
            WARMUP_STATE.update({"running": True, "ready": False, "error": ""})

        rows = _get_product_list()
        for product in rows:
            product_id = product.get("id")
            with CACHE_LOCK:
                if product_id and product_id in DETAILED_PRODUCT_IDS:
                    continue
            _load_full_product(product)
            # Держимся ниже лимита API и не душим МойСклад фоновым прогревом.
            time.sleep(0.07)

        with CACHE_LOCK:
            WARMUP_STATE["ready"] = True
            WARMUP_STATE["indexed"] = len(PACK_CACHE)
    except Exception as e:
        with CACHE_LOCK:
            WARMUP_STATE["error"] = str(e)
    finally:
        with CACHE_LOCK:
            WARMUP_STATE["running"] = False

def start_pack_warmup():
    with CACHE_LOCK:
        if WARMUP_STATE["running"] or WARMUP_STATE["ready"]:
            return
        WARMUP_STATE["running"] = True
    threading.Thread(target=_warm_pack_cache_worker, daemon=True).start()

def get_receipt(receipt_id):
    r = requests.get(
        f"{API_BASE}/entity/enter/{receipt_id}",
        headers=headers(),
        params={"expand": "positions"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def add_or_increment_position(receipt_id, product, pack, requested_qty=None, enforce_pack_limit=True):
    receipt = get_receipt(receipt_id)
    if receipt.get("applicable"):
        raise ValueError("Это Оприходование уже проведено. Открой черновик или создай новое.")

    positions = receipt.get("positions", {}).get("rows", []) or []
    product_href = product["meta"]["href"]
    standard_qty = float(pack["quantity"])
    add_qty = standard_qty if requested_qty is None else float(requested_qty)

    if add_qty <= 0:
        raise ValueError("Количество должно быть больше нуля.")
    if enforce_pack_limit and add_qty > standard_qty:
        raise ValueError(f"Количество {add_qty:g} больше стандартной палеты {standard_qty:g}.")

    for pos in positions:
        if pos.get("assortment", {}).get("meta", {}).get("href") == product_href:
            new_qty = float(pos.get("quantity", 0) or 0) + add_qty
            r = requests.put(
                f"{API_BASE}/entity/enter/{receipt_id}/positions/{pos['id']}",
                headers=headers(),
                json={"quantity": new_qty},
                timeout=30,
            )
            r.raise_for_status()
            return {"mode": "incremented", "added": add_qty, "total": new_qty}

    price = int(((product.get("buyPrice") or {}).get("value")) or 0)
    payload = {
        "quantity": add_qty,
        "price": price,
        "discount": 0,
        "vat": int(product.get("vat", 0) or 0),
        "assortment": {
            "meta": {
                "href": product_href,
                "type": product["meta"].get("type", "product"),
                "mediaType": "application/json",
            }
        },
    }

    r = requests.post(
        f"{API_BASE}/entity/enter/{receipt_id}/positions",
        headers=headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return {"mode": "created", "added": add_qty, "total": add_qty}

@app.get("/")
def index():
    return PAGE

@app.get("/api/health")
def health():
    return jsonify(ok=True, token_configured=bool(TOKEN))

@app.get("/api/bootstrap")
def bootstrap():
    try:
        return jsonify(**bootstrap_data())
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/receipts")
def receipts():
    try:
        return jsonify(receipts=list_receipts())
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/receipts")
def new_receipt():
    try:
        data = request.get_json(force=True)
        state_id = str(data.get("state_id", "")).strip()
        organization_id = str(data.get("organization_id", "")).strip()
        store_id = str(data.get("store_id", "")).strip()

        if not state_id or not organization_id or not store_id:
            return jsonify(error="Нужно выбрать склад, поставщика/статус и организацию"), 400

        doc = create_receipt(state_id, organization_id, store_id)

        state_name = ""
        store_name = ""
        try:
            state_name = (doc.get("state") or {}).get("name") or ""
            store_name = (doc.get("store") or {}).get("name") or ""
        except Exception:
            pass

        return jsonify(
            ok=True,
            id=doc.get("id"),
            name=doc.get("name") or "Новое Оприходование",
            state=state_name or "Статус выбран",
            store=store_name or "Склад выбран",
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/receipts/<receipt_id>/complete")
def finish_receipt(receipt_id):
    try:
        # Сводку считаем по самому документу МойСклад, чтобы финальный экран
        # не зависел от локальных счётчиков телефона.
        before = get_receipt(receipt_id)
        positions = (before.get("positions") or {}).get("rows", []) or []
        positions_count = len(positions)
        total_units = sum(float(x.get("quantity", 0) or 0) for x in positions)

        scan_row = query_one(
            """
            SELECT COUNT(*)
            FROM operation_events
            WHERE operation_type = ? AND event_type IN (?, ?) AND result = ?
            """,
            ("receiving", "scan", "external_pallet", receipt_id),
        )
        pallets_count = int(scan_row[0]) if scan_row and scan_row[0] is not None else 0

        doc = complete_receipt(receipt_id)
        employee = get_session_employee()
        if employee:
            log_event(
                employee["id"],
                "receiving",
                "complete",
                object_code=receipt_id,
                quantity=total_units,
                result=doc.get("name") or receipt_id,
            )

        # Целые количества показываем без .0.
        total_units_out = int(total_units) if float(total_units).is_integer() else total_units
        return jsonify(
            ok=True,
            id=doc.get("id"),
            name=doc.get("name") or "Оприходование",
            positions=positions_count,
            total_units=total_units_out,
            pallets=pallets_count,
            employee=(employee or {}).get("name", ""),
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/osz/check")
def osz_check():
    try:
        data = request.get_json(force=True)
        sscc = normalize_sscc(data.get("sscc", ""))
        existing = query_one(
            "SELECT receipt_id, article, quantity, accepted_at FROM external_pallets WHERE sscc = ?",
            (sscc,),
        )
        if existing:
            return jsonify(
                error=f"Палета ОСЗ SSCC {sscc} уже была принята ранее.",
                receipt_id=existing[0],
                article=existing[1],
                quantity=existing[2],
                accepted_at=existing[3],
            ), 409
        return jsonify(ok=True, sscc=sscc)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/osz/product-info")
def osz_product_info():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой ШК товара"), 400

        product, pack = find_product_by_any_barcode(barcode)
        if not product:
            return jsonify(error=f"Товар со штрихкодом {barcode} не найден"), 404

        standard_qty = float(pack.get("quantity") or 0)
        standard_qty_out = int(standard_qty) if standard_qty.is_integer() else standard_qty
        return jsonify(
            ok=True,
            barcode=barcode,
            name=product.get("name"),
            article=product.get("article"),
            pack_name=pack.get("name"),
            standard_qty=standard_qty_out,
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/osz/accept")
def osz_accept():
    try:
        data = request.get_json(force=True)
        receipt_id = str(data.get("receipt_id", "")).strip()
        sscc = normalize_sscc(data.get("sscc", ""))
        product_barcode = str(data.get("product_barcode", "")).strip()
        quantity = float(data.get("quantity", 0) or 0)

        if not receipt_id:
            return jsonify(error="Не выбрано Оприходование"), 400
        if not product_barcode:
            return jsonify(error="Не отсканирован товар"), 400
        if quantity <= 0:
            return jsonify(error="Количество должно быть больше нуля"), 400

        existing = query_one("SELECT receipt_id FROM external_pallets WHERE sscc = ?", (sscc,))
        if existing:
            return jsonify(error=f"Палета ОСЗ SSCC {sscc} уже принята."), 409

        product, pack = find_product_by_any_barcode(product_barcode)
        if not product:
            return jsonify(error=f"Товар со штрихкодом {product_barcode} не найден"), 404

        result = add_or_increment_position(
            receipt_id,
            product,
            pack,
            requested_qty=quantity,
            enforce_pack_limit=False,
        )

        employee = get_session_employee()
        execute(
            """
            INSERT INTO external_pallets
            (sscc, partner, receipt_id, employee_id, product_barcode, article, quantity, accepted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sscc,
                "OSZ",
                receipt_id,
                (employee or {}).get("id"),
                product_barcode,
                product.get("article"),
                result["added"],
                now_iso(),
            ),
        )

        if employee:
            log_event(
                employee["id"],
                "receiving",
                "external_pallet",
                object_code=sscc,
                quantity=result["added"],
                result=receipt_id,
            )

        added = result["added"]
        total = result["total"]
        if float(added).is_integer():
            added = int(added)
        if float(total).is_integer():
            total = int(total)

        return jsonify(
            ok=True,
            sscc=sscc,
            barcode=product_barcode,
            name=product.get("name"),
            article=product.get("article"),
            added=added,
            total=total,
        )
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/warmup")
def warmup():
    try:
        start_pack_warmup()
        with CACHE_LOCK:
            state = dict(WARMUP_STATE)
            state["cached_barcodes"] = len(PACK_CACHE)
        return jsonify(ok=True, **state)
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.get("/api/cache-status")
def cache_status():
    with CACHE_LOCK:
        return jsonify(
            ok=True,
            cached_barcodes=len(PACK_CACHE),
            detailed_products=len(DETAILED_PRODUCT_IDS),
            **WARMUP_STATE,
        )

@app.post("/api/pack-info")
def pack_info():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400

        product, pack = find_product_pack_by_barcode(barcode)
        if not product:
            return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        return jsonify(
            ok=True,
            barcode=barcode,
            name=product.get("name"),
            article=product.get("article"),
            pack_name=pack.get("name"),
            standard_qty=float(pack.get("quantity") or 0),
        )
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

@app.post("/api/scan")
def scan():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        receipt_id = str(data.get("receipt_id", "")).strip()
        requested_qty = data.get("quantity", None)

        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400
        if not receipt_id:
            return jsonify(error="Не выбрано Оприходование"), 400

        product, pack = find_product_pack_by_barcode(barcode)
        if not product:
            return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        result = add_or_increment_position(receipt_id, product, pack, requested_qty)
        employee = get_session_employee()
        if employee:
            log_event(
                employee["id"],
                "receiving",
                "scan",
                object_code=barcode,
                quantity=result["added"],
                result=receipt_id,
            )
        return jsonify(
            ok=True,
            barcode=barcode,
            name=product.get("name"),
            article=product.get("article"),
            pack_name=pack.get("name"),
            added=result["added"],
            total=result["total"],
            mode=result["mode"],
        )
    except ValueError as e:
        return jsonify(error=str(e)), 409
    except requests.HTTPError as e:
        body = e.response.text[:1200] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

try:
    init_wms_db()
except Exception as e:
    print(f"WMS DB init warning: {e}")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
