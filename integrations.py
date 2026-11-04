"""Read-only integration registry for GRIT WMS.

Phase one intentionally does not accept or persist API secrets. Live integrations
remain controlled by their existing environment/configuration and services.
"""
import os
from html import escape

from flask import Blueprint, Response, jsonify, redirect, request

from wms import manager_access
from marketplace_lab import AdapterError, DOCUMENTATION, SOURCES, run_scenario

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


@integrations_bp.get("/manager/integrations")
def integrations_page():
    if not manager_access():
        return redirect("/manager-login")
    cards = []
    for item in _catalog():
        if item["adapter"] == "existing":
            status = "Действующие настройки найдены" if item["configured"] else "Ключ не настроен"
            details = "Использует существующую конфигурацию GRIT. Проверка соединения и управление ключами пока не включены."
        else:
            status = "Адаптер запланирован"
            details = "Интеграция ещё не реализована. Подключение и ввод ключей станут доступны после разработки адаптера."
        cards.append(
            '<article class="card"><div class="between"><h2>' + escape(item["name"]) +
            '</h2><span class="status">' + escape(status) + '</span></div><p>' +
            escape(item["purpose"]) + '</p><small>' + escape(details) + '</small></article>'
        )
    html = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
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
p{line-height:1.5}small,.muted{color:#607388;line-height:1.5}.notice{padding:14px;border-left:3px solid #48647a;background:#f8fafc}
</style></head><body><main><header><div><div class="muted">GRIT WMS / Настройки</div>
<h1>Интеграции</h1></div><a href="/manager">← Панель управления</a></header>
<div class="notice">Первый этап: реестр интеграций. Токены не отображаются и не сохраняются на этой странице.
Новые сервисы нельзя подключить до реализации и проверки их адаптеров. Уже работающие соединения не изменены.</div>
<p><a href="/manager/integrations/lab">🧪 Открыть лабораторию маркетплейсов (без API-ключей)</a></p>\n<section class="grid">""" + "".join(cards) + """</section></main></body></html>"""
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
