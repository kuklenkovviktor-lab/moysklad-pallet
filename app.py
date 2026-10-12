import os
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
from flask import Flask, request, jsonify, session, redirect
from wms import (
    wms_bp,
    init_wms_db,
    get_session_employee,
    log_event,
    query_one,
    query_all,
    execute,
    now_iso,
    manager_access,
    WB_STRICT_MATCHING,
)
from storage_backup import (
    restore_if_needed,
    start_worker as start_backup_worker,
    backup_now,
    status as backup_status,
    RestoreRequiredError,
)

app = Flask(__name__)
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
if not SECRET_KEY:
    raise RuntimeError("SECRET_KEY is required in the environment")
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
)
app.register_blueprint(wms_bp)

@app.before_request
def require_employee_login():
    path = request.path
    public_paths = {
        "/login",
        "/api/auth/login",
        "/api/health",
        "/supervisor-terminal",
        "/api/supervisor-terminal/qr",
        "/manager-login",
        "/api/manager/login",
    }
    if path in public_paths or path.startswith("/static/") or path.startswith("/vendor/"):
        return None

    # Отдельная браузерная сессия руководителя для ПК.
    # Она даёт доступ только к desktop-панели и её API, без имитации сотрудника/TSD.
    manager_paths = {
        "/manager",
        "/stats",
        "/fbs-journal",
        "/history",
        "/badges",
        "/api/manager/logout",
        "/api/fbs/dashboard",
        "/api/fbs/overview",
        "/api/fbs/journal",
        "/api/fbs/hourly",
        "/api/history",
        "/api/stats/employees",
        "/api/admin/badges",
        "/api/admin/supervisor-exit-code",
        "/receiving-monitor",
        "/api/receiving/overview",
        "/api/wb/status",
        "/api/wb/sync",
        "/api/manager/daily-plan",
        "/api/operations/deviations",
        "/api/system/status",
    }
    # Desktop manager routes are authenticated only by the dedicated
    # manager password/session. Do not fall through to employee badge auth.
    if path in manager_paths:
        if session.get("manager_web") is True:
            return None
        if path.startswith("/api/"):
            return jsonify(error="Требуется вход в ПК-панель"), 403
        return redirect("/manager-login")

    employee = get_session_employee()
    if employee:
        return None
    if path.startswith("/api/"):
        return jsonify(error="Требуется вход по бейджу"), 401
    return redirect("/login")


API_BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN", "").strip()

WB_API_BASE = "https://marketplace-api.wildberries.ru"
WB_API_TOKEN = os.getenv("WB_API_TOKEN", "").strip()
WB_SYNC_DAYS = max(1, min(30, int(os.getenv("WB_SYNC_DAYS", "5") or 5)))
WB_SYNC_LOCK = threading.Lock()
WB_SYNC_STATE = {
    "running": False,
    "last_error": "",
    "last_started_at": None,
    "last_finished_at": None,
    "orders_seen": 0,
    "stickers_saved": 0,
}

# Быстрый индекс палетных штрихкодов в памяти Render.
# После первого прогрева скан не перебирает каталог МойСклад заново.
PACK_CACHE = {}
PRODUCT_BARCODE_CACHE = {}
PRODUCT_LIST_CACHE = {"ts": 0.0, "rows": []}
DETAILED_PRODUCT_IDS = set()
CACHE_LOCK = threading.RLock()
WARMUP_STATE = {"running": False, "ready": False, "indexed": 0, "error": ""}
PRODUCT_LIST_TTL = 3600
_BOOTSTRAP_CACHE = {"data": None, "expires_at": 0.0}
_BOOTSTRAP_CACHE_LOCK = threading.Lock()
BOOTSTRAP_CACHE_SECONDS = max(60, int(os.getenv("BOOTSTRAP_CACHE_SECONDS", "300") or 300))

def _wb_headers():
    return {
        "Authorization": "Bearer " + WB_API_TOKEN,
        "Content-Type": "application/json",
    }


def _wb_order_product(order):
    product = order.get("product") if isinstance(order.get("product"), dict) else order
    article = str(product.get("article") or order.get("article") or "").strip()
    raw_skus = product.get("skus") or order.get("skus") or []
    skus = [str(x).strip() for x in raw_skus if str(x).strip()]
    nm_id = product.get("nmId") or order.get("nmId")
    chrt_id = product.get("chrtId") or order.get("chrtId")
    return article, skus, nm_id, chrt_id


def _save_wb_order(order, sticker):
    order_id = str(sticker.get("orderId") or order.get("id") or "").strip()
    wb_barcode = str(sticker.get("barcode") or "").strip()
    if not order_id or not wb_barcode:
        return False

    article, skus, nm_id, chrt_id = _wb_order_product(order)
    supply_id = str(order.get("supplyId") or "").strip()
    synced_at = now_iso()
    existing = query_one("SELECT order_id FROM wb_orders WHERE order_id = ?", (order_id,))
    values = (
        wb_barcode,
        article or None,
        json.dumps(skus, ensure_ascii=False),
        str(nm_id) if nm_id is not None else None,
        str(chrt_id) if chrt_id is not None else None,
        supply_id or None,
        synced_at,
        order_id,
    )
    if existing:
        execute(
            """
            UPDATE wb_orders
            SET wb_barcode = ?, article = ?, skus_json = ?, nm_id = ?, chrt_id = ?,
                supply_id = ?, synced_at = ?
            WHERE order_id = ?
            """,
            values,
        )
    else:
        execute(
            """
            INSERT INTO wb_orders
            (wb_barcode, article, skus_json, nm_id, chrt_id, supply_id, synced_at, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return True


def sync_wb_orders():
    if not WB_API_TOKEN:
        raise RuntimeError("WB_API_TOKEN не настроен")

    with WB_SYNC_LOCK:
        WB_SYNC_STATE.update({
            "running": True,
            "last_error": "",
            "last_started_at": now_iso(),
            "orders_seen": 0,
            "stickers_saved": 0,
        })
        try:
            now_ts = int(time.time())
            date_from = now_ts - WB_SYNC_DAYS * 86400
            cursor = 0
            orders = {}
            seen_cursors = set()

            while True:
                r = requests.get(
                    WB_API_BASE + "/api/v3/orders",
                    headers=_wb_headers(),
                    params={
                        "limit": 1000,
                        "next": cursor,
                        "dateFrom": date_from,
                        "dateTo": now_ts,
                    },
                    timeout=30,
                )
                r.raise_for_status()
                payload = r.json()
                rows = payload.get("orders", []) or []
                for order in rows:
                    order_id = order.get("id")
                    if order_id is not None:
                        orders[str(order_id)] = order

                next_cursor = payload.get("next")
                if not rows or next_cursor in (None, 0, "0") or next_cursor == cursor:
                    break
                if str(next_cursor) in seen_cursors:
                    break
                seen_cursors.add(str(next_cursor))
                cursor = next_cursor
                time.sleep(0.22)

            existing_rows = query_all("SELECT order_id FROM wb_orders WHERE wb_barcode IS NOT NULL AND wb_barcode <> ''")
            existing_ids = {str(x[0]) for x in existing_rows}

            # Стикер доступен после привязки заказа к поставке. Повторно уже
            # известные стикеры не скачиваем: barcode заказа WB не меняется.
            pending_ids = [
                int(order_id)
                for order_id, order in orders.items()
                if order.get("supplyId") and order_id not in existing_ids
            ]

            saved = 0
            for i in range(0, len(pending_ids), 100):
                chunk = pending_ids[i:i + 100]
                r = requests.post(
                    WB_API_BASE + "/api/v3/orders/stickers",
                    headers=_wb_headers(),
                    params={"type": "zplv", "width": 40, "height": 30},
                    json={"orders": chunk},
                    timeout=30,
                )
                # Отдельная пачка не должна убить уже сохранённую синхронизацию.
                if r.status_code == 409:
                    time.sleep(0.22)
                    continue
                r.raise_for_status()
                for sticker in (r.json().get("stickers", []) or []):
                    order_id = str(sticker.get("orderId") or "")
                    order = orders.get(order_id)
                    if order and _save_wb_order(order, sticker):
                        saved += 1
                time.sleep(0.22)

            WB_SYNC_STATE.update({
                "last_finished_at": now_iso(),
                "orders_seen": len(orders),
                "stickers_saved": saved,
            })
        except Exception as exc:
            WB_SYNC_STATE["last_error"] = str(exc)
            WB_SYNC_STATE["last_finished_at"] = now_iso()
        finally:
            WB_SYNC_STATE["running"] = False


@app.get("/api/wb/status")
def wb_status():
    init_wms_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    count = query_one("SELECT COUNT(*) FROM wb_orders")
    last_sync = query_one("SELECT MAX(synced_at) FROM wb_orders")
    return jsonify(
        configured=bool(WB_API_TOKEN),
        strict=bool(WB_STRICT_MATCHING),
        local_orders=int(count[0] if count else 0),
        last_sync=(last_sync[0] if last_sync else None),
        sync_days=WB_SYNC_DAYS,
        **dict(WB_SYNC_STATE),
    )


@app.post("/api/wb/sync")
def wb_sync():
    init_wms_db()
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not WB_API_TOKEN:
        return jsonify(error="Сначала добавь WB_API_TOKEN в Render"), 503
    if WB_SYNC_STATE.get("running"):
        return jsonify(ok=True, running=True, message="Синхронизация WB уже идёт"), 202
    threading.Thread(target=sync_wb_orders, name="wb-fbs-sync", daemon=True).start()
    return jsonify(ok=True, running=True), 202


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
.qtypad{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
.qtypad button{margin-top:0;padding:13px;font-size:20px;background:#e9ebee;color:#111}
.qtypad button.action{font-size:16px}
.small{font-size:12px;color:#777}
.activeDoc{padding:13px;background:#f0f2f5;border-radius:12px;margin-top:10px}
.nav{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px}
.nav a{display:flex;align-items:center;justify-content:center;text-align:center;min-height:44px;padding:9px 8px;border-radius:12px;text-decoration:none;font-weight:700;font-size:14px;background:#e7edf2;color:#314657;border:1px solid #d2dde5}
.nav a.active{background:#48647a;color:#fff;border-color:#48647a}

@media (orientation:landscape) and (max-height:600px){
 body{overflow:hidden;background:#e9eef3;color:#25313d}
 .wrap{
  max-width:none;width:100%;height:100dvh;margin:0;
  padding:max(5px,env(safe-area-inset-top)) max(7px,env(safe-area-inset-right)) max(5px,env(safe-area-inset-bottom)) max(7px,env(safe-area-inset-left));
  display:grid;grid-template-columns:minmax(0,.9fr) minmax(0,1.1fr);
  grid-template-rows:auto auto auto minmax(0,1fr);gap:5px;overflow:hidden
 }
 .nav{grid-column:1/-1;display:flex;gap:5px;margin:0}
 .nav a{flex:1;min-height:30px;padding:5px 7px;border-radius:8px;font-size:10px}
 .wrap>.small{grid-column:1/-1;margin:0 2px!important;font-size:9px;line-height:1.1}
 .wrap>.card:first-of-type{
  grid-column:1/-1;margin:0;padding:6px 9px;border-radius:10px;
  display:flex;align-items:center;gap:10px;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb
 }
 .wrap>.card:first-of-type h1{font-size:15px;margin:0;white-space:nowrap}
 .wrap>.card:first-of-type .muted{font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #docCard{grid-column:1;grid-row:4;margin:0;min-width:0;height:100%;padding:8px;border-radius:10px;overflow:auto;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb}
 #scanCard{grid-column:2;grid-row:4;margin:0;min-width:0;height:100%;padding:8px;border-radius:10px;overflow:auto;box-shadow:none;border:1px solid #d7e0e7;background:#f7f9fb}
 #completionCard{position:fixed;inset:8px;z-index:50;margin:0;overflow:auto;border:2px solid #48647a}
 #docCard h2,#scanCard h2{font-size:13px;margin:0 0 4px}
 #docCard .muted,#scanCard .muted{font-size:9px;line-height:1.2}
 label{margin:5px 0 2px;font-size:9px}
 input,select{padding:7px 8px;font-size:11px;border-width:1px;border-radius:8px}
 button{padding:7px 8px;font-size:11px;border-radius:8px;margin-top:5px}
 .activeDoc{padding:7px;margin-top:5px;border-radius:8px;font-size:10px}
 #reader{margin-top:5px;max-height:130px;border-radius:9px}
 .ok,.err,.info{margin-top:5px;padding:7px;border-radius:8px;font-size:10px}
 .stats{margin-top:5px;padding:7px;border-radius:8px}.big{font-size:20px}
 .row{gap:5px}.qtypad{gap:4px;margin-top:5px}.qtypad button{padding:7px;font-size:14px}
 .small{font-size:9px}
}

.disabled-tab{opacity:.45;pointer-events:none}
.step-kicker{font-size:10px;font-weight:900;letter-spacing:.1em;color:#6b7785;margin-bottom:3px}
.scan-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:8px}
.scan-head h2{font-size:28px;margin:0}
.scan-counters{display:flex;gap:6px}
.scan-counters>div{min-width:72px;background:#eef3f6;border:1px solid #d7e0e7;border-radius:10px;padding:6px 8px;text-align:center}
.scan-counters span{display:block;font-size:22px;font-weight:900;line-height:1}
.scan-counters small{font-size:8px;color:#6b7785}
.quick-row{display:grid;grid-template-columns:repeat(3,1fr);gap:5px}
.quick-row button{margin-top:5px}
.finish-btn{margin-top:8px}
@media (orientation:landscape) and (max-height:600px){
 .wrap{grid-template-columns:1fr;grid-template-rows:auto auto 1fr}
 .nav{grid-column:1;grid-row:1}
 .wrap>.small{grid-column:1;grid-row:2}
 #docCard,#scanCard{grid-column:1;grid-row:3;width:min(900px,100%);justify-self:center;overflow:auto}
 #scanCard{display:block}
 .scan-head h2{font-size:24px}
 .scan-counters>div{min-width:62px;padding:5px}.scan-counters span{font-size:19px}
 .quick-row button{font-size:10px;padding:6px}
 #reader{max-height:115px}
}
</style>
</head>
<body>
<div class="wrap">
<div class="nav">
  <a class="active" href="/">📦 Приёмка</a>
  <a href="/fbs">🧺 Сборка</a>
  <a href="/move">↔ Перемещение</a>
</div>
<div class="small" style="margin:0 2px 10px"><span id="navIdentity"></span> · <a href="/exit">🚪 Завершить смену</a></div>

<div class="card" id="docCard">
  <div class="step-kicker">ПРИЁМКА</div>
  <h2 id="docTitle">Выбери документ</h2>
  <div id="activeDoc" class="activeDoc hidden"></div>

  <div id="docHome">
    <button id="newDocBtn" onclick="showNewDoc()">＋ Новая приёмка</button>
    <button class="secondary" onclick="showExisting()">Открыть существующую</button>
  </div>

  <div id="newDocForm" class="hidden">
    <label>Склад</label>
    <select id="storeSelect"><option value="">Загрузка…</option></select>
    <label>Поставщик</label>
    <select id="stateSelect"><option value="">Загрузка…</option></select>
    <label>Организация</label>
    <select id="orgSelect"><option value="">Загрузка…</option></select>
    <button onclick="createReceipt()">Начать приёмку</button>
    <button class="secondary" onclick="hideDocForms()">Назад</button>
  </div>

  <div id="existingForm" class="hidden">
    <label>Последние приёмки</label>
    <select id="receiptSelect"><option value="">Загрузка…</option></select>
    <button onclick="useSelectedReceipt()">Открыть</button>
    <button class="secondary" onclick="loadReceipts()">↻ Обновить</button>
    <button class="secondary" onclick="hideDocForms()">Назад</button>
  </div>
</div>

<div class="card hidden" id="scanCard">
  <div class="scan-head">
    <div>
      <div class="step-kicker">ПРИЁМКА</div>
      <h2 id="scanPrompt">Сканируй палету</h2>
      <div id="scanDocName" class="muted"></div>
    </div>
    <div class="scan-counters">
      <div><span id="pallets">0</span><small>палет</small></div>
      <div><span id="units">0</span><small>шт.</small></div>
    </div>
  </div>

  <button id="cameraBtn" onclick="toggleCamera('full')" disabled>📷 Полная палета</button>
  <div class="quick-row">
    <button class="secondary" id="partialBtn" onclick="toggleCamera('partial')" disabled>Неполная</button>
    <button class="secondary" id="oszBtn" onclick="startOszFlow()" disabled>ОСЗ / SSCC</button>
    <button class="secondary" id="manualBtn" onclick="toggleManual()" disabled>ТСД / ввод</button>
  </div>

  <div id="reader" class="hidden"></div>

  <div id="oszForm" class="info hidden">
    <b>ОСЗ / SSCC</b>
    <div class="small" id="oszStatus">Сканируй SSCC палеты.</div>
    <div id="oszProductInfo" class="hidden"></div>
    <label id="oszQtyLabel" class="hidden">Количество, шт.</label>
    <input id="oszQty" class="hidden" inputmode="numeric" type="number" min="1" step="1">
    <div id="oszQtyPad" class="qtypad hidden">
      <button type="button" onclick="oszQtyDigit('1')">1</button><button type="button" onclick="oszQtyDigit('2')">2</button><button type="button" onclick="oszQtyDigit('3')">3</button>
      <button type="button" onclick="oszQtyDigit('4')">4</button><button type="button" onclick="oszQtyDigit('5')">5</button><button type="button" onclick="oszQtyDigit('6')">6</button>
      <button type="button" onclick="oszQtyDigit('7')">7</button><button type="button" onclick="oszQtyDigit('8')">8</button><button type="button" onclick="oszQtyDigit('9')">9</button>
      <button type="button" class="action" onclick="oszQtyClear()">Очистить</button><button type="button" onclick="oszQtyDigit('0')">0</button><button type="button" class="action" onclick="oszQtyBackspace()">⌫</button>
    </div>
    <button id="oszProductBtn" class="secondary hidden" onclick="scanOszProduct()">Сканировать товар</button>
    <button id="oszAcceptBtn" class="hidden" onclick="acceptOszPallet()">Принять палету</button>
    <button class="secondary" onclick="cancelOszFlow()">Отмена</button>
  </div>

  <div id="manual" class="hidden">
    <input id="barcode" inputmode="numeric" autocomplete="off" placeholder="Сканируй штрихкод">
    <button onclick="sendManualFull()">Принять</button>
    <button class="secondary" onclick="sendManualPartial()">Неполная палета</button>
  </div>

  <div id="partialForm" class="info hidden">
    <b id="partialTitle">Неполная палета</b>
    <div class="small" id="partialMeta"></div>
    <label>Фактически, шт.</label>
    <input id="partialQty" inputmode="numeric" type="number" min="1" step="1">
    <button onclick="confirmPartial()">Принять количество</button>
    <button class="secondary" onclick="cancelPartial()">Отмена</button>
  </div>

  <div id="result"></div>
  <button id="completeBtn" class="danger finish-btn" onclick="completeReceipt()" disabled>✓ Завершить приёмку</button>
</div>

<div class="card hidden" id="completionCard">
  <div class="step-kicker">ГОТОВО</div>
  <h1>✅ Приёмка завершена</h1>
  <div id="completionSummary" class="stats"></div>
  <button onclick="startAnotherReceipt()">＋ Новая приёмка</button>
</div></div>

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

async function activateReceivingModule(){
  const shiftId=localStorage.getItem('fbs_shift_id')||'';
  if(!shiftId)return;
  try{
    await fetch('/api/fbs/activity',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({shift_id:shiftId,activity_type:'receiving'})
    });
  }catch(e){}
}
activateReceivingModule();

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
    box.innerHTML='<b>'+escapeHtml(activeReceiptName)+'</b><br><span class="small">'+escapeHtml(meta||'Активная приёмка')+'</span>';
    document.getElementById('scanDocName').textContent=activeReceiptName;
    document.getElementById('docCard').classList.add('hidden');
    document.getElementById('scanCard').classList.remove('hidden');
  }else{
    box.classList.add('hidden');
    box.innerHTML='';
    document.getElementById('scanDocName').textContent='';
    document.getElementById('scanCard').classList.add('hidden');
    document.getElementById('docCard').classList.remove('hidden');
  }
}

function showNewDoc(){
  document.getElementById('docHome').classList.add('hidden');
  document.getElementById('newDocForm').classList.remove('hidden');
  document.getElementById('existingForm').classList.add('hidden');
  document.getElementById('docTitle').textContent='Новая приёмка';
}
function showExisting(){
  document.getElementById('docHome').classList.add('hidden');
  document.getElementById('existingForm').classList.remove('hidden');
  document.getElementById('newDocForm').classList.add('hidden');
  document.getElementById('docTitle').textContent='Открыть приёмку';
  loadReceipts();
}
function hideDocForms(){
  document.getElementById('newDocForm').classList.add('hidden');
  document.getElementById('existingForm').classList.add('hidden');
  document.getElementById('docHome').classList.remove('hidden');
  document.getElementById('docTitle').textContent='Выбери документ';
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

  const scanWidth=Math.max(240,Math.min(360,window.innerWidth-56));
  const formats=(mode==='osz_sscc')
    ? [Html5QrcodeSupportedFormats.CODE_128]
    : [Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.CODE_128];

  document.getElementById('reader').classList.remove('hidden');
  document.getElementById('result').innerHTML='<div class="info">Наведи камеру на ШК целиком и держи его горизонтально.</div>';
  scanner=new Html5Qrcode("reader");

  try{
    await scanner.start(
      {facingMode:"environment"},
      {
        fps:20,
        qrbox:{width:scanWidth,height:110},
        disableFlip:true,
        formatsToSupport:formats
      },
      async(decodedText)=>{
        if(busy)return;
        const now=Date.now();
        if(decodedText===lastBarcode && now-lastScanAt<1800)return;
        lastBarcode=decodedText;
        lastScanAt=now;

        // Камеру закрываем сразу после распознавания, не ждём сеть/API.
        // Так пользователь мгновенно понимает, что код уже считан,
        // и камера не успевает повторно поймать тот же штрихкод.
        await stopScanner();
        document.getElementById('result').innerHTML='<div class="info"><b>ШК считан.</b> Обрабатываю…</div>';

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
    const message=String((e&&e.message)||e||'');
    if(message.includes('expected pattern')){
      showError('Safari не принял ускоренный режим камеры. Переключил на совместимый режим, попробуйте скан ещё раз.');
    }else{
      showError('Не удалось открыть камеру: '+message);
    }
  }
}

function looksLikeSscc(code){
  const raw=String(code||'').trim().replace(/\s+/g,'');
  if(raw.startsWith(']C1'))return true;
  if(/^\(00\)\d{18}$/.test(raw))return true;
  if(/^00\d{18}$/.test(raw))return true;
  if(/^\d{18}$/.test(raw))return true;
  return false;
}

async function sendManualFull(){
  const input=document.getElementById('barcode');
  const code=input.value.trim();
  if(!code)return;

  // Очищаем поле сразу, потому что HID-сканер уже передал весь код.
  // Дальше результат операции показывается отдельным статусом.
  input.value='';

  // Если уже считан SSCC ОСЗ, следующий HID-скан считаем ШК товара.
  if(oszSscc && !oszProductBarcode){
    document.getElementById('result').innerHTML='<div class="info"><b>ШК товара считан.</b> Ищу товар…</div>';
    await prepareOszProduct(code);
    return;
  }

  // HID-сканер может отдавать GS1 SSCC как (00)+18 цифр, 00+18 цифр
  // или только 18 цифр. Валидность окончательно проверяет сервер.
  if(looksLikeSscc(code)){
    document.getElementById('oszForm').classList.remove('hidden');
    document.getElementById('result').innerHTML='<div class="info"><b>SSCC считан.</b> Проверяю палету…</div>';
    await handleOszSscc(code,false);
    if(oszSscc){
      setTimeout(()=>input.focus(),80);
    }
    return;
  }

  await sendScan(code);
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
  document.getElementById('oszQtyPad').classList.add('hidden');
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

async function handleOszSscc(raw,autoCamera=true){
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
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>'+
      (autoCamera?'SSCC принят. Готовлю сканирование ШК товара…':'SSCC принят. Теперь отсканируйте ШК товара этим же сканером.');
    document.getElementById('oszProductBtn').classList.remove('hidden');
    successFeedback();

    // Для камеры следующий этап открываем автоматически.
    // Для Bluetooth/HID оставляем фокус в поле ТСД: следующий пик будет ШК товара.
    if(autoCamera){
      setTimeout(()=>{
        if(oszSscc && !scanner && !oszProductBarcode){
          toggleCamera('osz_product');
        }
      },250);
    }
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
    document.getElementById('oszQtyPad').classList.remove('hidden');
    document.getElementById('oszQty').value='';
    document.getElementById('oszAcceptBtn').classList.remove('hidden');
    document.getElementById('oszProductBtn').classList.add('hidden');
    document.getElementById('oszStatus').innerHTML='SSCC: <b>'+escapeHtml(oszSscc)+'</b><br>Проверьте количество по этикетке ОСЗ.';
    successFeedback();
  }catch(e){
    await stopScanner();
    showError(e.message);
  }finally{
    busy=false;
  }
}

function oszQtyDigit(digit){
  const input=document.getElementById('oszQty');
  const current=String(input.value||'');
  if(current.length>=7)return;
  input.value=(current==='0'?'':current)+digit;
}

function oszQtyBackspace(){
  const input=document.getElementById('oszQty');
  input.value=String(input.value||'').slice(0,-1);
}

function oszQtyClear(){
  document.getElementById('oszQty').value='';
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

    // При работе Bluetooth/HID сканером сразу готовы к следующей палете.
    const manual=document.getElementById('manual');
    if(manual && !manual.classList.contains('hidden')){
      setTimeout(()=>document.getElementById('barcode').focus(),80);
    }
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
    await fetch('/api/operation/enter',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({operation:'receiving'})});
  }catch(e){}
}
loadWmsIdentity();
loadBootstrap();
setTimeout(()=>fetch('/api/warmup',{method:'POST'}).catch(()=>{}),2500);
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
    now_ts = time.time()
    with _BOOTSTRAP_CACHE_LOCK:
        cached = _BOOTSTRAP_CACHE.get("data")
        expires_at = float(_BOOTSTRAP_CACHE.get("expires_at", 0) or 0)
        if cached is not None and expires_at > now_ts:
            return cached

    with ThreadPoolExecutor(max_workers=3) as pool:
        org_future = pool.submit(get_rows, "organization", {"limit": 1000, "order": "name"})
        store_future = pool.submit(get_rows, "store", {"limit": 1000, "order": "name"})
        state_future = pool.submit(get_enter_states)
        organizations = org_future.result()
        stores = store_future.result()
        states = state_future.result()

    data = {
        "organizations": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in organizations if not x.get("archived")],
        "stores": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in stores if not x.get("archived")],
        "states": [{"id": x["id"], "name": x.get("name") or "Без названия"} for x in states],
    }
    with _BOOTSTRAP_CACHE_LOCK:
        _BOOTSTRAP_CACHE["data"] = data
        _BOOTSTRAP_CACHE["expires_at"] = time.time() + BOOTSTRAP_CACHE_SECONDS
    return data

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

        # Прогреваем кеш спокойно, по одному товару.
        # Параллельный прогрев создавал всплеск запросов и мог выбить
        # лимит МойСклад, из-за чего обычные операции получали HTTP 429.
        for product in rows:
            product_id = product.get("id")
            with CACHE_LOCK:
                already_detailed = bool(product_id and product_id in DETAILED_PRODUCT_IDS)
            if already_detailed:
                continue

            try:
                _load_full_product(product)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429:
                    time.sleep(1.5)
                    try:
                        _load_full_product(product)
                    except Exception as retry_exc:
                        with CACHE_LOCK:
                            WARMUP_STATE["error"] = str(retry_exc)
                else:
                    with CACHE_LOCK:
                        WARMUP_STATE["error"] = str(exc)
            except Exception as exc:
                with CACHE_LOCK:
                    WARMUP_STATE["error"] = str(exc)

            # Не забиваем общий лимит API: приёмка важнее фонового прогрева.
            time.sleep(0.25)

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

def _system_status_payload():
    db_backend = "postgres" if os.getenv("DATABASE_URL", "").strip() else "sqlite"
    db_ok = False
    db_error = ""
    try:
        row = query_one("SELECT 1")
        db_ok = bool(row and row[0] == 1)
    except Exception as e:
        db_error = str(e)

    backup = backup_status()
    return {
        "ok": bool(db_ok and TOKEN),
        "token_configured": bool(TOKEN),
        "database_backend": db_backend,
        "database_ok": db_ok,
        "database_error": db_error,
        "backup": backup,
    }


@app.get("/api/health")
def health():
    # Public endpoint for Render/liveness checks. Do not expose infrastructure details.
    payload = _system_status_payload()
    return jsonify(ok=bool(payload["ok"])), (200 if payload["ok"] else 503)


@app.get("/api/system/status")
def system_status():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    return jsonify(**_system_status_payload())

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

        employee = get_session_employee()
        if employee:
            log_event(
                employee["id"],
                "receiving",
                "create",
                object_code=doc.get("id"),
                result=state_name or "",
                document_name=doc.get("name") or "Новое Оприходование",
            )

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
                document_name=doc.get("name") or receipt_id,
            )

        # После завершения приёмки сразу делаем аварийную копию WMS,
        # но не задерживаем ответ сотруднику ожиданием Object Storage.
        if not os.getenv("DATABASE_URL", "").strip():
            threading.Thread(target=backup_now, kwargs={"force": True}, daemon=True).start()

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


@app.post("/api/fbs/product-info")
def fbs_product_info():
    employee = get_session_employee()
    if not employee:
        return jsonify(error="Требуется вход по бейджу"), 401
    try:
        data = request.get_json(force=True)
        barcode = str(data.get("barcode", "")).strip()
        if not barcode:
            return jsonify(error="Пустой ШК товара"), 400

        product, pack = find_product_by_any_barcode(barcode)
        if not product:
            employee = get_session_employee()
            if employee:
                log_event(
                    employee["id"],
                    "fbs",
                    "product_not_found",
                    object_code=barcode,
                    result="not-found",
                )
            return jsonify(error=f"Товар со штрихкодом {barcode} не найден"), 404

        qty = float(pack.get("quantity") or 1)
        if qty > 1:
            qty_out = int(qty) if qty.is_integer() else qty
            return jsonify(
                error=f"Считан ШК упаковки на {qty_out} шт. Для FBS нужен ШК одной единицы товара."
            ), 409

        return jsonify(
            ok=True,
            barcode=barcode,
            article=product.get("article"),
            name=product.get("name"),
        )
    except requests.HTTPError as e:
        body = e.response.text[:800] if e.response is not None else ""
        return jsonify(error=f"Ошибка API МойСклад: {e}. {body}"), 502
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
                article=product.get("article"),
                product_name=product.get("name"),
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
                article=product.get("article"),
                product_name=product.get("name"),
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
    # На бесплатном Render локальный диск временный.
    # Если контейнер новый, сначала возвращаем последнюю SQLite-копию из Yandex Object Storage.
    restore_if_needed()
    init_wms_db()
    start_backup_worker()
except RestoreRequiredError as e:
    # Fail closed: если настроенный backup не восстановился, не создаём пустую БД.
    print(f"WMS restore blocked startup: {e}")
    raise
except Exception as e:
    print(f"WMS DB init warning: {e}")
    if os.getenv("WMS_REQUIRE_POSTGRES", "0").strip().lower() in ("1", "true", "yes", "on"):
        raise

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
