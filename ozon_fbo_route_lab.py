"""Isolated, synthetic-only Ozon FBO pick-order laboratory.

No reads from employee orders, no catalog/API calls and no write operations.
Dimensions are EXAMPLE external box dimensions in centimetres, not stock data.
"""
from flask import Blueprint, jsonify, redirect, request
from wms import manager_access
from warehouse_map import line_of, location_sort_key

route_lab_bp = Blueprint("fbo_route_lab", __name__)

# Explicitly fictional fixture: never infer these dimensions from an actual SKU.
SYNTHETIC_ITEMS = (
    {"article": "DEMO-BOX-XL", "name": "Большая коробка", "location": "А-04", "dimensions_cm": [50, 40, 35], "qty": 2},
    {"article": "DEMO-JAR-5L", "name": "Тара среднего размера", "location": "Б-08", "dimensions_cm": [25, 25, 32], "qty": 5},
    {"article": "DEMO-SHAMPOO", "name": "Небольшой флакон", "location": "Ж-12", "dimensions_cm": [8, 8, 22], "qty": 12},
    {"article": "DEMO-BOX-M", "name": "Средняя коробка", "location": "А-18", "dimensions_cm": [30, 25, 24], "qty": 3},
    {"article": "DEMO-PACK-S", "name": "Мелкая упаковка", "location": "И-03", "dimensions_cm": [12, 8, 6], "qty": 8},
)


def plan_route(items, mode="hybrid"):
    """Advice only. Address order is a proxy, NOT shortest physical route."""
    if mode not in ("size", "address", "hybrid"):
        raise ValueError("Неизвестный режим маршрута")
    prepared = []
    for item in items:
        dims = item.get("dimensions_cm")
        if (not isinstance(dims, (list, tuple)) or len(dims) != 3
                or any(type(v) not in (float, int) or v <= 0 or v > 1000 for v in dims)):
            raise ValueError("Для расчёта нужны три положительных габарита в сантиметрах")
        label = str(item.get("location") or "").strip().upper()
        if not label or len(label) > 40:
            raise ValueError("Требуется адрес ячейки")
        cm3 = dims[0] * dims[1] * dims[2]
        prepared.append({**item, "location": label, "volume_l": round(cm3 / 1000, 2),
                         "line": line_of(label)})
    def addr(i):
        return location_sort_key({"label": i["location"], "line": i["line"]})
    if mode == "size":
        ordered = sorted(prepared, key=lambda i: (-i["volume_l"], addr(i), i["article"]))
    elif mode == "address":
        ordered = sorted(prepared, key=lambda i: (addr(i), -i["volume_l"], i["article"]))
    else:
        # Large/medium/small groups. Within each group follow schematic address order.
        # Not a packing-stability guarantee; weight, crushability and walkways unknown.
        def group(i):
            return 0 if i["volume_l"] >= 30 else (1 if i["volume_l"] >= 5 else 2)
        ordered = sorted(prepared, key=lambda i: (group(i), addr(i), -i["volume_l"], i["article"]))
    switches = sum(a["line"] != b["line"] for a,b in zip(ordered, ordered[1:]))
    inversions = sum(a["volume_l"] < b["volume_l"] for a,b in zip(ordered, ordered[1:]))
    return {"mode": mode, "items": ordered, "line_switches": switches,
            "size_increases": inversions,
            "notice": "Адреса заданы условно: это не расстояние в метрах. Вес и прочность упаковки неизвестны."}


HTML = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e"><title>GRIT · Лаборатория маршрутов Ozon FBO</title>
<style>
:root{color-scheme:dark;--bg:#09121e;--card:#142337;--line:#30465c;--text:#f1f6ff;--muted:#9eb5ca}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,-apple-system,sans-serif}
main{max-width:920px;padding:18px;margin:auto}h1{font-size:26px;margin:8px 0}h2{font-size:17px;margin:0 0 10px}
a{color:#81c8ff}.small{font-size:12px;color:var(--muted)}
.card{background:linear-gradient(140deg,#16283b,#101c2a);border:1px solid var(--line);border-radius:13px;padding:17px;margin:14px 0}
.notice{border-left:3px solid #ff9230;padding:12px;background:#1a2b3d;border-radius:8px}
.controls{display:flex;gap:10px;flex-wrap:wrap;align-items:end;margin:14px 0}
label{display:grid;gap:5px;color:#c3d9eb;font-weight:700}
select,button{border-radius:9px;padding:11px;color:var(--text);font:inherit;min-height:42px}
select{background:#0d1d2c;border:1px solid #42607a}
button{background:#176eae;border:1px solid #358acc;font-weight:700;cursor:pointer}
button:hover{background:#2488cf}button:disabled{opacity:.55;cursor:wait}
button:focus-visible,select:focus-visible,a:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
.item{display:flex;gap:12px;padding:11px 0;border-top:1px solid var(--line);align-items:center}
.step{width:32px;height:32px;border-radius:8px;flex:none;background:#243e54;display:grid;place-items:center;font-weight:800}
.info{min-width:0;flex:1}.meta{color:var(--muted);font-size:12px}
.metrics{display:flex;gap:9px;flex-wrap:wrap}.metric{background:#1c3044;border-radius:9px;padding:10px 13px}
.metric strong{display:block;font-size:22px}
@media(max-width:620px){main{padding:12px}.controls label,.controls select,.controls button{width:100%}.card{padding:13px}}
</style></head><body><main>
<a href="/manager/ozon-fbo">← Ozon FBO</a>
<h1>Лаборатория порядка сборки</h1>
<p class="notice"><strong>ИЗОЛИРОВАННАЯ ДЕМОНСТРАЦИЯ</strong> · Только вымышленные товары, габариты и адреса.
Реальные задания, сканы и база склада не используются и не изменяются. Это не физический маршрут и не проверка устойчивости палеты.</p>
<section class="card"><h2>Алгоритм</h2><div class="controls">
<label>Порядок сборки<select id="mode">
<option value="hybrid">Умный: габаритная группа → адрес</option>
<option value="size">От большего к меньшему</option>
<option value="address">По адресу ячейки</option></select></label>
<button id="run">Рассчитать маршрут</button></div>
<div id="state" class="small" role="status">Выбери режим и запусти расчёт.</div></section>
<section class="card"><h2>Рекомендуемый порядок</h2>
<div id="metrics" class="metrics"></div><div id="items"></div></section>
<p class="small">Габариты здесь заданы вручную для демонстрации. Для реальных рекомендаций потребуется проверенная таблица
«артикул → Д×Ш×В», привязка к ячейке и схема проходов. Без веса и прочности упаковки результат только подсказка.</p>
<script>
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const btn=document.getElementById('run');
async function run(){
 btn.disabled=true;document.getElementById('state').textContent='Рассчитываю на синтетических данных…';
 try{
  const mode=document.getElementById('mode').value;
  const res=await fetch('/api/manager/ozon-fbo/route-lab?mode='+encodeURIComponent(mode),{cache:'no-store'});
  const d=await res.json();if(!res.ok)throw Error(d.error||'Ошибка расчёта');
  document.getElementById('state').textContent=d.notice;
  document.getElementById('metrics').innerHTML=
   '<div class="metric">Переходов между линиями<strong>'+d.line_switches+'</strong></div>'+
   '<div class="metric">Переходов к более крупным товарам<strong>'+d.size_increases+'</strong></div>';
  document.getElementById('items').innerHTML=d.items.map((i,index)=>
   '<div class="item"><div class="step">'+(index+1)+'</div><div class="info"><b>'+esc(i.name)+'</b><div class="meta">'+esc(i.article)+' · '+esc(i.location)+' · '+esc(i.dimensions_cm.join(' × '))+' см · '+esc(i.volume_l)+' л · '+esc(i.qty)+' шт.</div></div></div>').join('');
 }catch(e){document.getElementById('state').textContent=e.message}
 finally{btn.disabled=false}
}
btn.addEventListener('click',run);run();
</script></main></body></html>"""


@route_lab_bp.get("/manager/ozon-fbo/route-lab")
def route_lab_page():
    if not manager_access():
        return redirect("/manager-login")
    return HTML


@route_lab_bp.get("/api/manager/ozon-fbo/route-lab")
def route_lab_data():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    try:
        return jsonify({**plan_route(SYNTHETIC_ITEMS, request.args.get("mode", "hybrid")),
                        "synthetic": True, "source": "hardcoded_demo"})
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
