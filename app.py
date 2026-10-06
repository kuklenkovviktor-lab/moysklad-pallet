import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#111111">
<title>Приёмка палет</title>
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
</style>
</head>
<body>
<div class="wrap">

<div class="card">
  <h1>Приёмка палет</h1>
  <div class="muted">Создай Приёмку, выбери поставщика и склад, затем сканируй палеты. После работы нажми «Завершить приёмку».</div>
</div>

<div class="card" id="docCard">
  <h2>Документ Приёмки</h2>

  <div id="activeDoc" class="activeDoc hidden"></div>

  <button id="newDocBtn" onclick="showNewDoc()">＋ Новая приёмка</button>
  <button class="secondary" onclick="showExisting()">Открыть существующую</button>

  <div id="newDocForm" class="hidden">
    <label>Поставщик / контрагент</label>
    <select id="agentSelect"><option value="">Загрузка…</option></select>

    <label>Организация</label>
    <select id="orgSelect"><option value="">Загрузка…</option></select>

    <label>Склад</label>
    <select id="storeSelect"><option value="">Загрузка…</option></select>

    <button onclick="createReceipt()">Создать приёмку</button>
    <button class="secondary" onclick="hideDocForms()">Отмена</button>
  </div>

  <div id="existingForm" class="hidden">
    <label>Последние приёмки</label>
    <select id="receiptSelect"><option value="">Загрузка…</option></select>
    <button onclick="useSelectedReceipt()">Открыть выбранную</button>
    <button class="secondary" onclick="loadReceipts()">↻ Обновить список</button>
    <button class="secondary" onclick="hideDocForms()">Отмена</button>
  </div>
</div>

<div class="card" id="scanCard">
  <h2>Сканирование палет</h2>
  <div class="muted">Один скан палетного ШК добавляет количество из упаковки товара в активную Приёмку.</div>

  <button id="cameraBtn" onclick="toggleCamera()" disabled>📷 Сканировать камерой</button>
  <button class="secondary" id="manualBtn" onclick="toggleManual()" disabled>⌨️ Ввести код вручную / ТСД</button>

  <div id="reader" class="hidden"></div>

  <div id="manual" class="hidden">
    <label>Штрихкод палеты</label>
    <input id="barcode" inputmode="numeric" autocomplete="off" placeholder="Например 2000000000404">
    <button onclick="sendManual()">Добавить палету</button>
  </div>

  <div id="result"></div>

  <div class="stats">
    <div class="row">
      <div><div class="small">Палет принято</div><div class="big" id="pallets">0</div></div>
      <div><div class="small">Единиц товара</div><div class="big" id="units">0</div></div>
    </div>
  </div>

  <button id="completeBtn" class="danger" onclick="completeReceipt()" disabled>✓ Завершить приёмку</button>
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

const agentSelect=document.getElementById('agentSelect');
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

    fillSelect(agentSelect,data.counterparties,'Выбери поставщика');
    fillSelect(orgSelect,data.organizations,'Выбери организацию');
    fillSelect(storeSelect,data.stores,'Выбери склад');

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
  const agentId=agentSelect.value;
  const organizationId=orgSelect.value;
  const storeId=storeSelect.value;

  if(!agentId||!organizationId||!storeId){
    showError('Выбери поставщика, организацию и склад.');
    return;
  }

  try{
    const r=await fetch('/api/receipts',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({
        agent_id:agentId,
        organization_id:organizationId,
        store_id:storeId
      })
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось создать Приёмку');

    pallets=0; units=0;
    document.getElementById('pallets').textContent='0';
    document.getElementById('units').textContent='0';

    setActiveReceipt(data.id,data.name,data.agent+' · '+data.store+' · черновик');
    hideDocForms();
    document.getElementById('result').innerHTML='<div class="ok">Приёмка создана. Можно сканировать палеты.</div>';
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
  if(!confirm('Завершить и провести эту Приёмку? После этого она будет проведена в МойСклад.'))return;

  try{
    if(scanner){
      try{await scanner.stop();}catch(e){}
      scanner=null;
      document.getElementById('reader').classList.add('hidden');
      document.getElementById('cameraBtn').textContent='📷 Сканировать камерой';
    }

    const r=await fetch('/api/receipts/'+activeReceiptId+'/complete',{method:'POST'});
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Не удалось завершить Приёмку');

    document.getElementById('result').innerHTML='<div class="ok"><b>'+escapeHtml(data.name)+'</b><br>Приёмка проведена в МойСклад.</div>';
    setActiveReceipt('','','');
  }catch(e){
    showError(e.message);
  }
}

function toggleManual(){
  initAudio();
  document.getElementById('manual').classList.toggle('hidden');
  setTimeout(()=>document.getElementById('barcode').focus(),100);
}

async function toggleCamera(){
  initAudio();
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}

  if(scanner){
    try{await scanner.stop();}catch(e){}
    scanner=null;
    document.getElementById('reader').classList.add('hidden');
    document.getElementById('cameraBtn').textContent='📷 Сканировать камерой';
    return;
  }

  if(typeof Html5Qrcode==='undefined'){
    showError('Модуль сканирования не загрузился. Обнови страницу.');
    return;
  }

  document.getElementById('reader').classList.remove('hidden');
  scanner=new Html5Qrcode("reader");

  try{
    await scanner.start(
      {facingMode:"environment"},
      {fps:10,qrbox:{width:280,height:140},formatsToSupport:[
        Html5QrcodeSupportedFormats.EAN_13,
        Html5QrcodeSupportedFormats.CODE_128,
        Html5QrcodeSupportedFormats.QR_CODE
      ]},
      async(decodedText)=>{if(!busy)await sendScan(decodedText);},
      ()=>{}
    );
    document.getElementById('cameraBtn').textContent='⏹ Остановить камеру';
  }catch(e){
    scanner=null;
    document.getElementById('reader').classList.add('hidden');
    document.getElementById('cameraBtn').textContent='📷 Сканировать камерой';
    showError('Не удалось открыть камеру: '+e);
  }
}

async function sendManual(){
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;
  await sendScan(code);
  input.value='';
  input.focus();
}

async function sendScan(barcode){
  if(busy)return;
  if(!activeReceiptId){showError('Сначала создай или открой Приёмку.');return;}
  busy=true;

  try{
    const r=await fetch('/api/scan',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode,receipt_id:activeReceiptId})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Ошибка');

    pallets+=1;
    units+=Number(data.added||0);
    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;

    successFeedback();

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
  if(e.key==='Enter')sendManual();
});

loadBootstrap();
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

def bootstrap_data():
    organizations = get_rows("organization", {"limit": 1000, "order": "name"})
    stores = get_rows("store", {"limit": 1000, "order": "name"})
    counterparties = get_rows("counterparty", {"limit": 1000, "order": "name"})

    return {
        "organizations": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in organizations if not x.get("archived")],
        "stores": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in stores if not x.get("archived")],
        "counterparties": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in counterparties if not x.get("archived")],
    }

def list_receipts():
    rows = get_rows("supply", {
        "limit": 30,
        "order": "moment,desc",
        "expand": "agent,store",
    })
    result = []
    for row in rows:
        moment = (row.get("moment") or "")[:16].replace("T", " ")
        name = row.get("name") or "Без номера"
        agent = (row.get("agent") or {}).get("name") or "без поставщика"
        store = (row.get("store") or {}).get("name") or "без склада"
        status = "проведена" if row.get("applicable") else "черновик"
        result.append({
            "id": row.get("id"),
            "name": name,
            "label": f"{name} · {moment} · {agent} · {status}",
            "meta": f"{agent} · {store} · {status}",
        })
    return result

def create_receipt(agent_id, organization_id, store_id):
    payload = {
        "organization": meta("organization", organization_id),
        "agent": meta("counterparty", agent_id),
        "store": meta("store", store_id),
        "applicable": False,
        "description": "Приёмка создана мобильным сканером палет",
    }
    r = requests.post(
        f"{API_BASE}/entity/supply",
        headers=headers(),
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def complete_receipt(receipt_id):
    r = requests.put(
        f"{API_BASE}/entity/supply/{receipt_id}",
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

def _pack_matches_barcode(pack, barcode):
    target = str(barcode).strip()
    candidates = []
    candidates.extend(_barcode_values(pack.get("barcode")))
    candidates.extend(_barcode_values(pack.get("barcodes")))
    for value in candidates:
        if str(value).strip() == target:
            return True
    return False

def find_product_pack_by_barcode(barcode):
    target = str(barcode).strip()
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

        for product in rows:
            packs = product.get("packs", []) or []

            # Сначала проверяем упаковки, если они уже пришли в списке товаров.
            for pack in packs:
                if _pack_matches_barcode(pack, target):
                    return product, pack

            # В списочном ответе МойСклад некоторые поля могут быть неполными.
            # Поэтому при необходимости читаем полную карточку товара.
            product_id = product.get("id")
            if product_id:
                d = requests.get(
                    f"{API_BASE}/entity/product/{product_id}",
                    headers=headers(),
                    timeout=30,
                )
                d.raise_for_status()
                full_product = d.json()
                for pack in full_product.get("packs", []) or []:
                    if _pack_matches_barcode(pack, target):
                        return full_product, pack

        if len(rows) < limit:
            break
        offset += limit

    return None, None

def get_receipt(receipt_id):
    r = requests.get(
        f"{API_BASE}/entity/supply/{receipt_id}",
        headers=headers(),
        params={"expand": "positions"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

def add_or_increment_position(receipt_id, product, pack):
    receipt = get_receipt(receipt_id)
    if receipt.get("applicable"):
        raise ValueError("Эта Приёмка уже проведена. Открой черновик или создай новую.")

    positions = receipt.get("positions", {}).get("rows", []) or []
    product_href = product["meta"]["href"]
    add_qty = float(pack["quantity"])

    for pos in positions:
        if pos.get("assortment", {}).get("meta", {}).get("href") == product_href:
            new_qty = float(pos.get("quantity", 0) or 0) + add_qty
            r = requests.put(
                f"{API_BASE}/entity/supply/{receipt_id}/positions/{pos['id']}",
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
        f"{API_BASE}/entity/supply/{receipt_id}/positions",
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
        agent_id = str(data.get("agent_id", "")).strip()
        organization_id = str(data.get("organization_id", "")).strip()
        store_id = str(data.get("store_id", "")).strip()

        if not agent_id or not organization_id or not store_id:
            return jsonify(error="Нужно выбрать поставщика, организацию и склад"), 400

        doc = create_receipt(agent_id, organization_id, store_id)

        agent_name = ""
        store_name = ""
        try:
            agent_name = (doc.get("agent") or {}).get("name") or ""
            store_name = (doc.get("store") or {}).get("name") or ""
        except Exception:
            pass

        return jsonify(
            ok=True,
            id=doc.get("id"),
            name=doc.get("name") or "Новая Приёмка",
            agent=agent_name or "Поставщик выбран",
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
        doc = complete_receipt(receipt_id)
        return jsonify(ok=True, id=doc.get("id"), name=doc.get("name") or "Приёмка")
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

        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400
        if not receipt_id:
            return jsonify(error="Не выбрана Приёмка"), 400

        product, pack = find_product_pack_by_barcode(barcode)
        if not product:
            return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        result = add_or_increment_position(receipt_id, product, pack)
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
