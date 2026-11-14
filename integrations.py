"""Read-only integration registry for GRIT WMS.

Phase one intentionally does not accept or persist API secrets. Live integrations
remain controlled by their existing environment/configuration and services.
"""
import os
import hmac
import json
import secrets
from datetime import datetime, timezone
import requests
from html import escape

from flask import Blueprint, Response, jsonify, redirect, request, session

from wms import manager_access
from marketplace_lab import AdapterError, DOCUMENTATION, SOURCES, run_scenario
from marketplace_http import BASES, MarketplaceHTTPError, mock_sync
from marketplace_http_journal import PersistentJournal

integrations_bp = Blueprint("integrations", __name__)


def _catalog():
    return [
        {
            "id": "moysklad",
            "name": "МойСклад",
            "purpose": "Каталог, документы, приёмка и складские операции",
            "configured": bool(os.getenv("MOYSKLAD_TOKEN", "").strip()),
            "adapter": "existing",
        },
        {
            "id": "wildberries",
            "name": "Wildberries",
            "purpose": "FBS-заказы, поставки и статусы в рамках прав API",
            "configured": bool(os.getenv("WB_API_TOKEN", "").strip()),
            "adapter": "existing",
        },
        {
            "id": "ozon",
            "name": "Ozon",
            "purpose": "Отправления, товары и операции",
            "configured": False,
            "adapter": "planned",
        },
        {
            "id": "yandex",
            "name": "Яндекс Маркет",
            "purpose": "Заказы, поставки и статусы",
            "configured": False,
            "adapter": "planned",
        },
        {
            "id": "chestny_znak",
            "name": "Честный ЗНАК",
            "purpose": "КИЗ, Data Matrix, статусы маркировки и документы",
            "configured": False,
            "adapter": "planned",
        },
    ]


@integrations_bp.get("/api/manager/integrations")
def integrations_list_api():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    return jsonify(integrations=_catalog(), secrets_exposed=False)


def _moysklad_error_codes(response):
    """Expose only numeric MoySklad error IDs; never its raw error messages."""
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return []
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        return []
    codes = []
    for entry in errors[:8]:
        if not isinstance(entry, dict):
            continue
        code = entry.get("code")
        if type(code) is int and 0 <= code <= 999999 and code not in codes:
            codes.append(code)
    return codes


def _moysklad_health():
    """One explicit read-only probe. Never return credentials or upstream body."""
    token = os.getenv("MOYSKLAD_TOKEN", "").strip()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not token:
        return {"state": "unconfigured", "label": "Не настроено",
                "checked_at": checked_at, "details": "Токен МойСклад не задан."}
    try:
        response = requests.get(
            "https://api.moysklad.ru/api/remap/1.2/entity/product",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                     "Accept-Encoding": "gzip"},
            params={"limit": 1},
            timeout=8,
            allow_redirects=False,
        )
    except (requests.Timeout, requests.ConnectionError):
        return {"state": "unavailable", "label": "Нет соединения",
                "checked_at": checked_at, "details": "МойСклад не ответил. Проверь сеть и повтори проверку."}
    except requests.RequestException:
        return {"state": "unavailable", "label": "Ошибка проверки",
                "checked_at": checked_at, "details": "Запрос к МойСклад не выполнен."}
    if response.status_code == 200:
        try:
            valid = isinstance(response.json().get("rows"), list)
        except (ValueError, AttributeError):
            valid = False
        if valid:
            return {"state": "connected", "label": "Подключено · API работает",
                    "checked_at": checked_at, "details": "Каталог товаров доступен для чтения."}
        return {"state": "unavailable", "label": "Неожиданный ответ",
                "checked_at": checked_at, "details": "Ответ API не соответствует ожидаемому формату."}
    if response.status_code in (401, 403):
        return {"state": "unauthorized", "label": "Нет доступа к API",
                "checked_at": checked_at, "details": "Проверь права или актуальность токена."}
    if response.status_code == 429:
        return {"state": "unavailable", "label": "Лимит запросов",
                "checked_at": checked_at, "details": "МойСклад ограничил частоту запросов. Повтори позже."}
    codes = _moysklad_error_codes(response)
    diagnostic = (" · код ошибки МойСклад: " + ", ".join(map(str, codes))) if codes else (
        " · код ошибки в ответе отсутствует")
    return {"state": "unavailable", "label": "API не отвечает штатно",
            "checked_at": checked_at,
            "details": "МойСклад вернул HTTP " + str(response.status_code) + diagnostic + "."}


@integrations_bp.post("/api/manager/integrations/moysklad/check")
def moysklad_connection_check():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    # The request is explicitly triggered from the manager UI; does not mutate catalog.
    return jsonify(_moysklad_health())


@integrations_bp.get("/manager/integrations")
def integrations_page():
    if not manager_access():
        return redirect("/manager-login")
    cards = []
    for item in _catalog():
        if item["adapter"] == "existing":
            status = "Настройки найдены · не проверено" if item["configured"] else "Ключ не настроен"
            details = "Использует существующую конфигурацию GRIT. Подключение проверяется по запросу руководителя, ключи не отображаются."
        else:
            status = "Адаптер запланирован"
            details = "Интеграция ещё не реализована. Подключение и ввод ключей станут доступны после разработки адаптера."
        extra = ('<div class="health-controls"><button id="checkMoysklad" type="button">Проверить соединение</button>'
                 '<div id="moyskladResult" class="health-result" role="status" aria-live="polite">'
                 'Проверка ещё не выполнялась.</div></div>') if item["id"] == "moysklad" else ""
        cards.append(
            '<article class="card"><div class="between"><h2>' + escape(item["name"]) +
            '</h2><span class="status" id="' + escape(item["id"]) + '-status">' + escape(status) + '</span></div><p>' +
            escape(item["purpose"]) + '</p><small>' + escape(details) + '</small>' + extra + '</article>'
        )
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e">
<title>GRIT WMS · Интеграции</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#e8edf2;color:#203247;font:15px Arial,sans-serif}
main{max-width:1050px;margin:32px auto;padding:0 18px}
header{display:flex;justify-content:space-between;align-items:center;gap:18px;flex-wrap:wrap}
a{color:#345a73;text-decoration:none;font-weight:700}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(290px,1fr));gap:14px;margin:22px 0}
.card{background:#f8fafc;border:1px solid #d6e0e7;border-radius:12px;padding:20px}
.between{display:flex;align-items:start;justify-content:space-between;gap:8px;flex-wrap:wrap}
h1{margin:8px 0}h2{margin:0;font-size:19px}.status{background:#e7edf2;border-radius:6px;padding:6px;font-size:12px}
p{line-height:1.5}small,.muted{color:#9eb5ca;line-height:1.5}.notice{padding:14px;border-left:3px solid #176eae;background:#142337}
:root{color-scheme:dark}
html,body{background:#09121e;color:#f1f6ff}
.card{background:linear-gradient(145deg,#16283b,#101c2a);border-color:#30465c}
a{color:#81c8ff}.status{background:#233b50;color:#cde7fa;border:1px solid #365976}
button{background:#176eae;color:#fff;border:1px solid #358acc;border-radius:9px;padding:11px 14px;font-weight:700;cursor:pointer;min-height:42px}
button:disabled{opacity:.55;cursor:wait}
button:hover{background:#2488cf}
button:focus-visible,a:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
.health-controls{margin-top:16px;display:flex;align-items:center;flex-wrap:wrap;gap:12px}
.health-result{color:#9eb5ca;font-size:13px;line-height:1.55;overflow-wrap:anywhere}
.health-result.connected{color:#81e9b9}.health-result.unavailable,.health-result.unauthorized{color:#ffb68d}
.health-result.unconfigured{color:#a7bdd0}
.status.connected{background:#173b35;color:#81e9b9;border-color:#2e705b}
.status.unavailable,.status.unauthorized{background:#463126;color:#ffb68d;border-color:#81583d}
@media(max-width:620px){main{margin:18px auto;padding:0 12px}.card{padding:16px}.health-controls button{width:100%}}
</style></head><body><main><header><div><div class="muted">GRIT WMS / Настройки</div>
<h1>Интеграции</h1></div><a href="/manager">← Панель управления</a></header>
<div class="notice">Первый этап: реестр интеграций. Токены не отображаются и не сохраняются на этой странице.
Новые сервисы нельзя подключить до реализации и проверки их адаптеров. Уже работающие соединения не изменены.</div>
<p><a href="/manager/integrations/lab">🧪 Открыть лабораторию маркетплейсов (без API-ключей)</a></p><p><a href="/manager/integrations/http">🌐 HTTP-адаптеры и журнал синхронизации (тестовый контур)</a></p>\n<section class="grid">""" + "".join(cards) + """</section>
<script>
const check=document.getElementById('checkMoysklad');
if(check){
 check.addEventListener('click',async()=>{
  if(check.disabled)return;
  check.disabled=true;
  const output=document.getElementById('moyskladResult');
  const badge=document.getElementById('moysklad-status');
  output.textContent='Проверяем доступ к каталогу…';output.className='health-result';
  try{
   const r=await fetch('/api/manager/integrations/moysklad/check',{
    method:'POST',cache:'no-store',credentials:'same-origin',
    headers:{'Accept':'application/json'}
   });
   const data=await r.json();
   if(!r.ok)throw Error(data.error||'Не удалось проверить подключение');
   const state=['connected','unconfigured','unauthorized','unavailable'].includes(data.state)?data.state:'unavailable';
   badge.textContent=data.label;badge.className='status '+state;
   output.className='health-result '+state;
   const date=data.checked_at?new Date(data.checked_at).toLocaleString('ru-RU'):'';
   output.textContent=data.details+(date?' · Проверено: '+date:'');
  }catch(e){
   output.className='health-result unavailable';
   output.textContent='Не удалось выполнить проверку. '+e.message;
  }finally{check.disabled=false}
 });
}
</script></main></body></html>"""
    return Response(html, content_type="text/html; charset=utf-8")


@integrations_bp.get("/api/manager/integrations/lab")
def integration_lab_api():
    """Deterministic read-only test responses. Never touches the WMS database."""
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    scenario = request.args.get("scenario", "normal")
    try:
        result = run_scenario(scenario)
    except AdapterError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(result)


@integrations_bp.get("/manager/integrations/lab")
def integration_lab_page():
    if not manager_access():
        return redirect("/manager-login")
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e"><title>GRIT WMS · Лаборатория интеграций</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#09121e;color:#edf6ff;font:14px/1.5 system-ui,Arial,sans-serif}
main{max-width:1300px;margin:auto;padding:24px}
a{color:#81c8ff}h1{font-size:28px;margin:12px 0 4px}h2{font-size:18px;margin:0 0 12px}
p{color:#a9c1d6}.sub{font-size:13px;color:#a9c1d6}
.banner,.panel{border:1px solid #36536b;background:linear-gradient(145deg,#15283b,#0f1e2d);border-radius:13px;padding:19px;margin:15px 0}
.banner{border-left:4px solid #f79730}
.controls{display:flex;gap:9px;flex-wrap:wrap;margin:14px 0}
button{font:inherit;font-weight:680;background:#1b6eab;color:#fff;border:1px solid #368fcd;border-radius:9px;min-height:42px;padding:10px 14px;cursor:pointer}
button:hover{background:#2587c7}
button.active{background:#f79730;color:#18202b;border-color:#ffb65f}
button:focus-visible,a:focus-visible{outline:2px solid #f79730;outline-offset:2px}
.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.metric{background:#1b3449;border:1px solid #35546d;border-radius:9px;padding:13px}
.metric small{display:block;color:#adc6db}.metric strong{font-size:26px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:760px}
th,td{border-bottom:1px solid #2f4b60;padding:11px;text-align:left;font-size:13px}
th{background:#1d3449;color:#cae3f3}
code{color:#ffba72;overflow-wrap:anywhere}
.pass{color:#59d59b}.fail{color:#ff8181}
ul{padding-left:22px}li{padding:4px 0}
@media(max-width:600px){main{padding:12px}.metrics{grid-template-columns:1fr}}
</style></head><body><main>
<a href="/manager/integrations">← Все интеграции</a>
<h1>Лаборатория маркетплейсов</h1>
<p class="sub">GRIT WMS · Wildberries / Ozon / Яндекс Маркет</p>
<section class="banner"><strong>ИЗОЛИРОВАННЫЙ СИМУЛЯТОР · НЕ РЕАЛЬНАЯ ИНТЕГРАЦИЯ</strong>
<p>Только синтетические заказы. Нет API-токенов, сетевых запросов, записи в БД и изменений заказов на маркетплейсах. Сценарий воспроизводится заново при каждом запуске.</p></section>
<div class="controls" id="buttons">
<button data-scenario="normal" class="active">Обычные заказы</button>
<button data-scenario="duplicates">Повторная загрузка</button>
<button data-scenario="isolation">Два селлера</button>
<button data-scenario="invalid">Ошибка количества</button>
<button data-scenario="rate_limit">Лимит API 429</button>
</div>
<div class="metrics" id="metrics" aria-live="polite"></div>
<section class="panel"><h2>Единая очередь заказов · тестовые данные</h2>
<div id="state" class="sub" role="status">Запуск теста…</div>
<div class="scroll"><table><thead><tr><th>Маркетплейс</th><th>Селлер</th><th>Заказ</th><th>Статус</th><th>Артикулы</th><th>Шт.</th></tr></thead><tbody id="orders"></tbody></table></div>
</section>
<section class="panel"><h2>Результаты проверок</h2><ul id="checks"></ul></section>
<section class="panel"><h2>Документация API</h2>
<p>WB: <code>GET /api/v3/orders/new</code> · Ozon: <code>POST /v4/posting/fbs/list</code> · Яндекс: <code>POST /v1/businesses/{businessId}/orders</code></p>
<p class="sub">Следующий этап: реальные HTTP-адаптеры и отдельные разрешения продавцов. Тестовый режим не является подтверждением работы с настоящими API.</p>
</section>
</main><script>
const E=id=>document.getElementById(id);
function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
let requestSeq=0;
async function run(scenario){
 const seq=++requestSeq;
 document.querySelectorAll('button[data-scenario]').forEach(b=>b.classList.toggle('active',b.dataset.scenario===scenario));
 E('state').textContent='Выполняем тест…';
 try{
  const r=await fetch('/api/manager/integrations/lab?scenario='+encodeURIComponent(scenario),{cache:'no-store'});
  if(r.status===403){location.href='/manager-login';return}
  const d=await r.json();
  if(seq!==requestSeq)return;
  if(!r.ok)throw Error(d.error||'Ошибка лаборатории');
  E('state').innerHTML=d.passed?'<strong class="pass">Тест пройден</strong>':'<strong class="fail">Тест не пройден</strong>';
  const sellers=new Set(d.orders.map(x=>x.seller_id)).size;
  E('metrics').innerHTML=[['Заказов',d.orders.length],['Единиц',d.total_units],['Селлеров',sellers]].map(x=>'<div class="metric"><small>'+x[0]+'</small><strong>'+x[1]+'</strong></div>').join('');
  E('orders').innerHTML=d.orders.length?d.orders.map(o=>'<tr><td>'+esc(o.platform)+'</td><td>'+esc(o.seller_id)+'</td><td>'+esc(o.external_id)+'</td><td>'+esc(o.status)+'</td><td>'+esc(o.items.map(i=>i.article+' ×'+i.quantity).join(', '))+'</td><td>'+o.total_units+'</td></tr>').join(''):'<tr><td colspan="6">Нет заказов для этого сценария</td></tr>';
  E('checks').innerHTML=d.checks.map(c=>'<li>'+esc(c)+'</li>').join('');
 }catch(e){if(seq===requestSeq){E('state').textContent=e.message;E('state').className='fail'}}
}
E('buttons').addEventListener('click',e=>{const b=e.target.closest('button[data-scenario]');if(b)run(b.dataset.scenario)});
run('normal');
</script></body></html>""";
    return Response(html, content_type="text/html; charset=utf-8")


@integrations_bp.get("/api/manager/integrations/http/journal")
def http_sync_journal_api():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    try:
        entries = PersistentJournal().recent(limit=40)
    except Exception:
        return jsonify(error="Не удалось прочитать журнал синхронизации"), 503
    return jsonify(entries=entries, mode="metadata_only", durable=True)


@integrations_bp.post("/api/manager/integrations/http/mock")
def http_mock_run_api():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    expected = session.get("grit_mp_http_csrf", "")
    given = request.headers.get("X-GRIT-CSRF", "")
    if not expected or not hmac.compare_digest(str(expected), str(given)):
        return jsonify(error="Недействительный токен формы"), 403
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Ожидается JSON"), 400
    provider = body.get("provider")
    scenario = body.get("scenario", "ok")
    if not isinstance(provider, str) or provider not in BASES:
        return jsonify(error="Неизвестный маркетплейс"), 400
    if not isinstance(scenario, str) or scenario not in {
        "ok", "unauthorized", "rate_limit", "rate_limit_long", "server_error", "invalid_json"
    }:
        return jsonify(error="Неизвестный тест"), 400
    try:
        result = mock_sync(provider, scenario, journal=PersistentJournal())
        # Do not return raw headers, auth or full fake payloads.
        return jsonify(ok=True, provider=provider, scenario=scenario,
                       count=result["count"], attempts=result["attempts"],
                       no_real_network=True)
    except MarketplaceHTTPError as exc:
        # Controlled failure scenarios are valuable test results, not HTTP 500.
        return jsonify(ok=False, provider=provider, scenario=scenario,
                       error_code=exc.category, status=exc.status,
                       retry_after=exc.retry_after, no_real_network=True)
    except Exception:
        return jsonify(error="Не удалось выполнить тест HTTP-адаптера"), 503


@integrations_bp.get("/manager/integrations/http")
def http_lab_page():
    if not manager_access():
        return redirect("/manager-login")
    token = secrets.token_urlsafe(32)
    session["grit_mp_http_csrf"] = token
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e">
<title>GRIT WMS · HTTP-адаптеры маркетплейсов</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#09121e;color:#e9f3fd;font:14px/1.5 system-ui,-apple-system,Arial,sans-serif}
main{max-width:1230px;padding:24px;margin:auto}
a{color:#83c7fc}h1{font-size:27px;margin:14px 0 4px}h2{font-size:18px;margin:0 0 12px}
p,.sub{color:#abc2d6;font-size:13px}
.card{padding:18px;background:linear-gradient(145deg,#162a3d,#102031);border:1px solid #35536c;border-radius:12px;margin:15px 0}
.banner{border-left:4px solid #ff9638}
.controls{display:flex;align-items:end;flex-wrap:wrap;gap:12px}
label{display:grid;gap:6px;color:#bdd4e6;font-size:12px;font-weight:650}
select,button{font:inherit;border-radius:9px;min-height:42px;padding:9px 12px}
select{background:#10253a;color:#f3f8ff;border:1px solid #49687e;min-width:170px}
button{background:#1978b9;color:#fff;border:1px solid #4599d3;cursor:pointer;font-weight:700}
button:disabled{opacity:.5;cursor:wait}button:hover{background:#238aca}
button:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid #ff993b;outline-offset:2px}
.scroll{overflow:auto}table{width:100%;min-width:880px;border-collapse:collapse}
th,td{padding:11px;border-bottom:1px solid #29485e;text-align:left;font-size:12px}
th{background:#1d3449;color:#c7ddee}
code{color:#ffc082}.ok{color:#62dca3}.err{color:#ff9696}
@media(max-width:650px){main{padding:12px}.controls{align-items:stretch;flex-direction:column}.controls select,.controls button{width:100%}}
</style></head><body><main>
<a href="/manager/integrations">← Интеграции GRIT</a>
<h1>HTTP-адаптеры маркетплейсов</h1>
<p>Wildberries · Ozon · Яндекс Маркет</p>
<section class="card banner"><strong>ТЕСТОВЫЙ КОНТУР · ВНЕШНЯЯ СЕТЬ ОТКЛЮЧЕНА</strong>
<p>Запускаются настоящие обработчики HTTP-запросов через подменённый транспорт. Селлерские ключи не нужны. Никаких операций с реальными заказами. Журнал тестов сохраняется отдельно от складских заказов.</p>
<p>Реальные чтения доступны только через серверный программный интерфейс после отдельного явного включения и предоставления разрешённых учётных данных. На этой странице включить их невозможно.</p></section>
<section class="card"><h2>Тест HTTP-клиента</h2>
<div class="controls"><label>Маркетплейс<select id="provider"><option value="wildberries">Wildberries</option><option value="ozon">Ozon</option><option value="yandex">Яндекс Маркет</option></select></label>
<label>Сценарий<select id="scenario"><option value="ok">Успешный ответ 200</option><option value="rate_limit">429 → повтор → 200</option><option value="rate_limit_long">429, длительная задержка</option><option value="unauthorized">401, нет доступа</option><option value="server_error">503, сбой API</option><option value="invalid_json">200, повреждённый JSON</option></select></label>
<button id="run">Запустить проверку</button></div>
<p id="result" role="status">Выбери сценарий и нажми «Запустить проверку».</p></section>
<section class="card"><h2>Журнал синхронизации · метаданные</h2>
<p>Сохраняются время, маркетплейс, тестовый продавец, исход, HTTP-статус, попытки и количество заказов. Токены, покупатели и ответы API не записываются.</p>
<button id="reload" type="button">Обновить журнал</button>
<div class="scroll"><table><thead><tr><th>Время UTC</th><th>Площадка</th><th>Режим</th><th>Результат</th><th>HTTP</th><th>Попытки</th><th>Заказы</th><th>Код ошибки</th></tr></thead>
<tbody id="entries"><tr><td colspan="8">Загрузка…</td></tr></tbody></table></div></section>
</main><script>
const TOKEN=__CSRF_TOKEN__;
const E=id=>document.getElementById(id);
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function loadJournal(){
 try{
  const r=await fetch('/api/manager/integrations/http/journal',{cache:'no-store'});
  if(r.status===403){location.href='/manager-login';return}
  const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка журнала');
  E('entries').innerHTML=d.entries.length?d.entries.map(x=>'<tr><td>'+esc(x.at)+'</td><td>'+esc(x.platform)+'</td><td>'+esc(x.mode)+'</td><td class="'+(x.status==='ok'?'ok':'err')+'">'+esc(x.status)+'</td><td>'+esc(x.http_status||'—')+'</td><td>'+esc(x.attempts)+'</td><td>'+esc(x.orders)+'</td><td>'+esc(x.error_code||'—')+'</td></tr>').join(''):'<tr><td colspan="8">Запусков пока нет</td></tr>';
 }catch(e){E('entries').innerHTML='<tr><td colspan="8">'+esc(e.message)+'</td></tr>'}
}
E('run').addEventListener('click',async()=>{
 E('run').disabled=true;E('result').textContent='Выполняется тест…';
 try{
  const r=await fetch('/api/manager/integrations/http/mock',{
   method:'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-GRIT-CSRF':TOKEN},
   body:JSON.stringify({provider:E('provider').value,scenario:E('scenario').value})
  });
  if(r.status===403){location.href='/manager-login';return}
  const d=await r.json();if(!r.ok)throw Error(d.error||'Ошибка');
  E('result').className=d.ok?'ok':'err';
  E('result').textContent=d.ok?'Успешно: '+d.count+' заказов, попыток '+d.attempts:
   'Контролируемая ошибка: '+d.error_code+' (HTTP '+(d.status||'—')+')';
  await loadJournal();
 }catch(e){E('result').className='err';E('result').textContent=e.message}
 finally{E('run').disabled=false}
});
E('reload').addEventListener('click',loadJournal);
loadJournal();
</script></body></html>""".replace("__CSRF_TOKEN__", json.dumps(token))
    response = Response(html, content_type="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-store"
    return response
