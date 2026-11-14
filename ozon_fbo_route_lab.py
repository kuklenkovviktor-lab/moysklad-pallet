"""Isolated, synthetic-only Ozon FBO pick-order laboratory.

No reads from employee orders, no catalog/API calls and no write operations.
Dimensions are EXAMPLE external box dimensions in centimetres, not stock data.
"""
from flask import Blueprint, jsonify, redirect, request
from wms import manager_access, query_all
from ozon_fbo_dimensions import read_dimensions_xlsx, parse_synthetic_basket, join_virtual_basket
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
textarea,input[type=file]{background:#0d1d2c;color:#f1f6ff;border:1px solid #42607a;border-radius:9px;padding:11px;width:100%}
textarea{min-height:110px;font:inherit}.issues{border-left:3px solid #eaa15b;padding:8px 12px;background:#312c2c;margin-top:10px}
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
<section class="card"><h2>Связать справочник и карту склада · виртуальный предпросмотр</h2>
<p class="small">Экспортируй Google-таблицу «Справочник NEW +» в формате XLSX. Читаем вкладки «Габариты» и «Сводный»,
а адреса получаем из существующих привязок карты GRIT только для просмотра. Данные не сохраняются; рабочие задания не изменяются.</p>
<p class="small">Используй экспорт собственного разрешённого справочника. Ссылка на личную Google-таблицу намеренно не хранится в коммерческом приложении.</p>
<form id="joinedForm" enctype="multipart/form-data">
<label>Экспорт таблицы (.xlsx)<input type="file" name="catalog" accept=".xlsx" required></label>
<label>Синтетическая палета: артикул;количество, одна позиция в строке
<textarea name="basket" placeholder="1022_BOROCCO;2&#10;1088_NONE;1" required></textarea></label>
<label>Алгоритм<select name="mode"><option value="hybrid">Габаритная группа → адрес</option>
<option value="size">От крупного к мелкому</option><option value="address">По адресу</option></select></label>
<button type="submit" id="joinedBtn">Проверить связи и рассчитать</button></form>
<div class="small" id="joinedStatus" role="status">Проверка работает без изменения заказов и операций склада.</div>
<div id="joinedReport"></div></section>
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
const form=document.getElementById('joinedForm');
form.addEventListener('submit',async e=>{
 e.preventDefault();const b=document.getElementById('joinedBtn'),state=document.getElementById('joinedStatus'),
 report=document.getElementById('joinedReport');b.disabled=true;state.textContent='Проверяю XLSX и привязки…';report.innerHTML='';
 try{
  const res=await fetch('/api/manager/ozon-fbo/route-lab/preview',{method:'POST',body:new FormData(form),cache:'no-store'});
  const d=await res.json();if(!res.ok)throw Error(d.error||'Не удалось рассчитать');
  state.textContent='Сопоставлено '+d.matched+' из '+d.requested+
   ' позиций; габаритов в справочнике: '+d.catalog_complete+
   '; строк с неполными габаритами: '+d.catalog_incomplete+'.';
  const issues=d.issues.length?'<div class="issues"><b>Требуют проверки</b>'+
   d.issues.map(i=>'<div>'+esc(i.article)+' · '+esc(i.reason)+'</div>').join('')+'</div>':'';
  const items=d.route?d.route.items.map((i,index)=>
   '<div class="item"><div class="step">'+(index+1)+'</div><div class="info"><b>'+esc(i.article)+'</b>'+
   '<div class="meta">'+esc(i.location)+' · '+esc(i.volume_l)+' л · '+esc(i.weight_kg??'Вес не указан')+
   ' кг · '+esc(i.qty)+' шт. · ШК: '+esc(i.barcodes.join(', ')||'нет в Сводном')+'</div></div></div>').join(''):'<p class="small">Нет позиций с одновременно известными габаритами и адресом. Маршрут не рассчитан.</p>';
  report.innerHTML=issues+'<h2 style="margin-top:16px">Виртуальный порядок по связанным позициям</h2>'+items;
 }catch(err){state.textContent=err.message}
 finally{b.disabled=false}
});
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


def _map_assignments_for_preview():
    """Read-only current schematic bindings, without stock/production modifications."""
    return query_all("""
        SELECT a.article,l.label
        FROM cell_sku_assignments a
        JOIN replenishment_locations l ON l.id=a.location_id
        WHERE l.active=1
    """)


@route_lab_bp.post("/api/manager/ozon-fbo/route-lab/preview")
def route_lab_joined_preview():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if request.content_length and request.content_length > 3 * 1024 * 1024 + 15000:
        return jsonify(error="Файл слишком большой"), 413
    upload = request.files.get("catalog")
    if not upload or not str(upload.filename or "").lower().endswith(".xlsx"):
        return jsonify(error="Загрузи XLSX справочника"), 400
    try:
        catalog = read_dimensions_xlsx(upload.read(3 * 1024 * 1024 + 1))
        basket = parse_synthetic_basket(request.form.get("basket", ""))
        mode = request.form.get("mode", "hybrid")
        if mode not in ("hybrid", "size", "address"):
            raise ValueError("Некорректный алгоритм")
        located, issues = join_virtual_basket(catalog, basket, _map_assignments_for_preview())
        route = plan_route(located, mode) if located else None
        return jsonify({
            "synthetic_basket": True, "read_only": True, "saved": False,
            "requested": len(basket), "matched": len(located),
            "catalog_complete": len(catalog["items"]),
            "catalog_incomplete": len(catalog["incomplete"]),
            "issues": issues, "route": route,
        })
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except Exception:
        return jsonify(error="Не удалось прочитать карту склада для предпросмотра"), 503
