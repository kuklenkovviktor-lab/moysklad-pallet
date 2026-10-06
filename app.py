import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
DEFAULT_RECEIPT_ID = os.getenv("MOYSKLAD_RECEIPT_ID", "").strip()

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
.wrap{max-width:560px;margin:0 auto;padding:18px}
.card{background:#fff;border-radius:18px;padding:18px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:26px;margin:0 0 8px}
.muted{color:#666;font-size:14px;line-height:1.35}
label{display:block;margin:14px 0 6px;font-size:13px;color:#666}
input{width:100%;padding:15px;font-size:18px;border:2px solid #d0d4d9;border-radius:12px}
button{width:100%;padding:16px;font-size:18px;font-weight:700;border:0;border-radius:12px;cursor:pointer;background:#111;color:#fff;margin-top:10px}
button.secondary{background:#e9ebee;color:#111}
#reader{width:100%;margin-top:14px;overflow:hidden;border-radius:14px}
.hidden{display:none}
.ok,.err{margin-top:14px;padding:14px;border-radius:12px}
.ok{background:#eaf8ee}
.err{background:#fdecec}
.stats{margin-top:14px;padding:14px;background:#f0f2f5;border-radius:12px}
.big{font-size:28px;font-weight:800}
.row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.small{font-size:12px;color:#777}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
<h1>Приёмка палет</h1>
<div class="muted">1 скан палетного ШК = количество из упаковки МойСклад</div>

<label>ID Приёмки МойСклад</label>
<input id="receiptId" value="__DEFAULT_RECEIPT__" placeholder="UUID документа Приёмки">

<button id="cameraBtn" onclick="toggleCamera()">📷 Сканировать камерой</button>
<button class="secondary" onclick="toggleManual()">⌨️ Ввести код вручную / ТСД</button>

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
</div>
</div>

<script>
let scanner=null;
let pallets=0;
let units=0;
let busy=false;

const receiptInput=document.getElementById('receiptId');
receiptInput.value=localStorage.getItem('receipt_id') || receiptInput.value || '';
receiptInput.addEventListener('change',()=>localStorage.setItem('receipt_id',receiptInput.value.trim()));

function toggleManual(){
  document.getElementById('manual').classList.toggle('hidden');
  setTimeout(()=>document.getElementById('barcode').focus(),100);
}

async function toggleCamera(){
  if(scanner){
    try{await scanner.stop();}catch(e){}
    scanner=null;
    document.getElementById('reader').classList.add('hidden');
    document.getElementById('cameraBtn').textContent='📷 Сканировать камерой';
    return;
  }

  if(typeof Html5Qrcode === 'undefined'){
    showError('Модуль сканирования не загрузился. Обнови страницу и попробуй ещё раз.');
    return;
  }

  document.getElementById('reader').classList.remove('hidden');
  scanner=new Html5Qrcode("reader");

  try{
    await scanner.start(
      {facingMode:"environment"},
      {
        fps:10,
        qrbox:{width:280,height:140},
        formatsToSupport:[
          Html5QrcodeSupportedFormats.EAN_13,
          Html5QrcodeSupportedFormats.CODE_128,
          Html5QrcodeSupportedFormats.QR_CODE
        ]
      },
      async(decodedText)=>{
        if(!busy) await sendScan(decodedText);
      },
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
  busy=true;

  const receiptId=receiptInput.value.trim();
  localStorage.setItem('receipt_id',receiptId);

  if(!receiptId){
    showError('Сначала укажи ID Приёмки.');
    busy=false;
    return;
  }

  try{
    const r=await fetch('/api/scan',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode:barcode,receipt_id:receiptId})
    });

    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Ошибка');

    pallets+=1;
    units+=Number(data.added||0);

    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;

    if(navigator.vibrate)navigator.vibrate(100);

    document.getElementById('result').innerHTML=
      '<div class="ok"><b>'+(data.article||data.name||'')+'</b><br>'+
      'Добавлено: <b>'+data.added+' шт.</b><br>'+
      'Теперь в Приёмке: <b>'+data.total+' шт.</b><br>'+
      '<span class="small">'+(data.pack_name||'')+' · '+data.barcode+'</span></div>';
  }catch(e){
    showError(e.message);
  }finally{
    setTimeout(()=>{busy=false;},900);
  }
}

function showError(message){
  document.getElementById('result').innerHTML='<div class="err">'+String(message)+'</div>';
}

document.getElementById('barcode').addEventListener('keydown',e=>{
  if(e.key==='Enter')sendManual();
});
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

def find_product_pack_by_barcode(barcode):
    url = f"{API_BASE}/entity/product"
    limit = 1000
    offset = 0

    while True:
        r = requests.get(
            url,
            headers=headers(),
            params={"limit": limit, "offset": offset},
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json().get("rows", [])

        for product in rows:
            for pack in product.get("packs", []) or []:
                if str(pack.get("barcode", "")).strip() == barcode:
                    return product, pack

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

    buy_price = product.get("buyPrice", {}) or {}
    price = int(buy_price.get("value", 0) or 0)

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
    return PAGE.replace("__DEFAULT_RECEIPT__", DEFAULT_RECEIPT_ID)

@app.get("/api/health")
def health():
    return jsonify(ok=True, token_configured=bool(TOKEN))

@app.post("/api/scan")
def scan():
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        receipt_id = str(data.get("receipt_id", "") or DEFAULT_RECEIPT_ID).strip()

        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400
        if not receipt_id:
            return jsonify(error="Не указан ID Приёмки"), 400

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
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.text[:1200]
        except Exception:
            pass
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
