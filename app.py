import os
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()
RECEIPT_ID = os.getenv("MOYSKLAD_RECEIPT_ID", "").strip()

PAGE = '''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Приёмка палет</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f5f6f8}
.wrap{max-width:560px;margin:0 auto;padding:20px}
.card{background:#fff;border-radius:16px;padding:18px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:24px;margin:0 0 8px}
.muted{color:#666;font-size:14px}
input{width:100%;box-sizing:border-box;padding:16px;font-size:20px;margin:14px 0 10px;border:2px solid #ccc;border-radius:12px}
button{width:100%;padding:16px;font-size:18px;border:0;border-radius:12px;cursor:pointer;background:#111;color:#fff}
.ok{margin-top:14px;padding:14px;background:#eaf8ee;border-radius:12px}
.err{margin-top:14px;padding:14px;background:#fdecec;border-radius:12px}
.stats{margin-top:14px;padding:14px;background:#f0f2f5;border-radius:12px}
.big{font-size:28px;font-weight:700}
</style>
</head>
<body>
<div class="wrap">
<div class="card">
<h1>Приёмка палет</h1>
<div class="muted">1 скан палетного ШК = количество из упаковки МойСклад</div>
<input id="barcode" inputmode="numeric" autocomplete="off" placeholder="Сканируй ШК палеты">
<button onclick="sendScan()">Добавить палету</button>
<div id="result"></div>
<div class="stats">
<div>Палет принято: <span id="pallets" class="big">0</span></div>
<div>Единиц товара: <span id="units" class="big">0</span></div>
</div>
</div>
</div>
<script>
let pallets=0,units=0;
const input=document.getElementById('barcode');

async function sendScan(){
  const barcode=input.value.trim();
  if(!barcode)return;
  const result=document.getElementById('result');
  result.textContent='Отправляю…';
  try{
    const r=await fetch('/api/scan',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({barcode})
    });
    const data=await r.json();
    if(!r.ok)throw new Error(data.error||'Ошибка');
    pallets+=1;
    units+=Number(data.added||0);
    document.getElementById('pallets').textContent=pallets;
    document.getElementById('units').textContent=units;
    result.innerHTML='<div class="ok"><b>'+(data.article||'')+'</b><br>'+
      data.name+'<br>Палета: <b>'+data.added+' шт.</b><br>'+
      'В приёмке теперь: <b>'+data.total+' шт.</b></div>';
    input.value='';
    input.focus();
  }catch(e){
    result.innerHTML='<div class="err">'+e.message+'</div>';
  }
}
input.addEventListener('keydown',e=>{if(e.key==='Enter')sendScan()});
input.focus();
</script>
</body>
</html>'''

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
        data = r.json()
        rows = data.get("rows", [])
        for product in rows:
            for pack in product.get("packs", []) or []:
                if str(pack.get("barcode", "")) == barcode:
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
            new_qty = float(pos.get("quantity", 0)) + add_qty
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
    return PAGE

@app.post("/api/scan")
def scan():
    try:
        if not RECEIPT_ID:
            return jsonify(error="MOYSKLAD_RECEIPT_ID не задан"), 500

        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой штрихкод"), 400

        product, pack = find_product_pack_by_barcode(barcode)
        if not product:
            return jsonify(error=f"Упаковка со штрихкодом {barcode} не найдена"), 404

        result = add_or_increment_position(RECEIPT_ID, product, pack)
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
    app.run(host="0.0.0.0", port=5000, debug=True)
