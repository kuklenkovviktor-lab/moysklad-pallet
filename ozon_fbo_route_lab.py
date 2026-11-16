"""Isolated, synthetic-only Ozon FBO pick-order laboratory.

No reads from employee orders, no catalog/API calls and no write operations.
Dimensions are EXAMPLE external box dimensions in centimetres, not stock data.
"""
from flask import Blueprint, jsonify, redirect, request
from wms import manager_access, query_all
from ozon_fbo_dimensions import read_dimensions_xlsx, parse_synthetic_basket, join_virtual_basket
from grit_google_sheets import get_google_catalog, sync_configuration, GoogleSheetsSyncError
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
            "unique_lines": len({i["line"] for i in ordered}),
            "notice": "Схематический маршрут по адресам: расстояния в метрах и проходы не измерены. Вес и прочность упаковки неизвестны."}


def recommend_route(located):
    """Select a transparent schematic compromise, not a physical shortest path.

    Minimize aisle-line changes first, then increases in box volume; prefer
    hybrid when tied. Scoring uses only synthesized basket positions and
    schematic warehouse addresses, no travel distances or live operations.
    """
    candidates = {mode: plan_route(located, mode)
                  for mode in ("hybrid", "size", "address")}
    priority = {"hybrid": 0, "address": 1, "size": 2}
    chosen = min(candidates, key=lambda mode: (
        candidates[mode]["line_switches"],
        candidates[mode]["size_increases"],
        priority[mode]))
    return chosen, candidates


def make_virtual_picking_sheet(located, issues, mode):
    """Read-only manager-only Ozon FBO pick-list; never creates a real picking job.

    Source addresses are provided by the internal map lookup; dimensions only
    from authorized Google Sheets. The printed copy is conspicuously a test.
    """
    recommended_mode = None
    if mode == "auto":
        recommended_mode, _candidates = recommend_route(located) if located else ("hybrid", {})
        mode = recommended_mode
    planned = plan_route(located, mode) if located else None
    rows = []
    for index, item in enumerate(planned["items"] if planned else (), 1):
        rows.append({
            "step": index, "article": item["article"],
            "location": item["location"], "line": item["line"],
            "qty": item["qty"], "dimensions_cm": item["dimensions_cm"],
            "volume_l": item["volume_l"], "status": "planned",
        })
    return {"title": "Ozon FBO · Виртуальный сборочный лист",
            "simulation_only": True, "printable": True,
            "mode": mode, "recommended_mode": recommended_mode,
            "rows": rows, "excluded": list(issues),
            "positions": len(rows),
            "units": sum(row["qty"] for row in rows),
            "location_source": "grit_warehouse_map",
            "dimension_source": "google_sheets_articles_dimensions_only",
            "distance_optimized": False}



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
.sheet-toolbar{display:flex;gap:10px;justify-content:space-between;align-items:center;flex-wrap:wrap;margin:12px 0}
.sheet-toolbar button{background:#28485f}
.pick-sheet{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
.pick-sheet th,.pick-sheet td{padding:10px 8px;border-bottom:1px solid #30465c;text-align:left}
.pick-sheet th{color:#aac4d9;font-size:12px}
.pick-sheet .cell-code{font-size:16px;color:#f9b869;font-weight:800}
.pick-sheet tr:nth-child(even){background:#1b2d40}
.pick-meta{color:#aac4d9;font-size:12px;margin-bottom:10px}
.sheet-demo{font-weight:800;color:#ffba79}
@media print{body{background:white!important;color:black!important}main{max-width:none;padding:0}
main>*,section.card{display:none!important}#joinedSection{display:block!important;border:0!important;background:white!important;padding:0!important}
#joinedSection>h2,#joinedSection>p,#joinedSection>.actions,#joinedSection form,#joinedStatus{display:none!important}
#joinedReport>*:not(#printSheet){display:none!important}#printSheet{display:block!important}
#printSheet *{color:black!important;background:white!important;border-color:#777!important}
#printSheet button{display:none!important}.sheet-demo{font-size:18px!important}}

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
<section class="card" id="joinedSection"><h2>Виртуальный сборочный лист Ozon FBO</h2>
<p class="small">GRIT читает только артикул и три габарита из вкладки «Товары» нашей Google-таблицы «Новая таблица» (только чтение). Справочник автоматически обновляется при обращении, не чаще одного раза в 15 минут на процесс. Рабочие задания не меняются.</p>
<div class="actions"><button type="button" id="syncBtn">↻ Синхронизировать Google Sheets</button><span class="small" id="syncState">Проверяю подключение…</span></div>
<form id="joinedForm">
<label>Синтетическая палета: артикул;количество, одна позиция в строке
<textarea name="basket" placeholder="1022_BOROCCO;2&#10;1088_NONE;1" required></textarea></label>
<label>Алгоритм<select name="mode"><option value="auto">Рекомендовать по схеме склада</option><option value="hybrid">Габаритная группа → адрес</option>
<option value="size">От крупного к мелкому</option><option value="address">По адресу</option></select></label>
<button type="submit" id="joinedBtn">Проверить связи и рассчитать</button></form>
<div class="small" id="joinedStatus" role="status">Проверка работает без изменения заказов и операций склада.</div>
<div id="joinedReport"></div></section>
<section class="card"><h2>Рекомендуемый порядок</h2>
<div id="metrics" class="metrics"></div><div id="items"></div></section>
<p class="small">Демонстрационный набор выше использует вымышленные габариты. Для рекомендаций используется подключённая Google-таблица
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
async function syncGoogle(force=false){
 const label=document.getElementById('syncState'),button=document.getElementById('syncBtn');
 button.disabled=true;label.textContent='Проверяю подключение Google Sheets…';
 try{
  const res=await fetch('/api/manager/ozon-fbo/route-lab/google-sync'+(force?'?force=1':''),{
   method:force?'POST':'GET',cache:'no-store'});
  const d=await res.json();
  if(!res.ok)throw Error(d.error||'Не удалось получить справочник');
  label.textContent='Синхронизировано: '+d.catalog_complete+' артикулов с габаритами · '+
   d.catalog_incomplete+' неполных · '+new Date(d.synced_at).toLocaleString('ru-RU')+
   (d.cached?' · кэш':'');
 }catch(err){label.textContent=err.message}
 finally{button.disabled=false}
}
document.getElementById('syncBtn').addEventListener('click',()=>syncGoogle(true));
syncGoogle();
const form=document.getElementById('joinedForm');
form.addEventListener('submit',async e=>{
 e.preventDefault();const b=document.getElementById('joinedBtn'),state=document.getElementById('joinedStatus'),
 report=document.getElementById('joinedReport');b.disabled=true;state.textContent='Проверяю Google Sheets и привязки…';report.innerHTML='';
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
   '<div class="meta">'+esc(i.location)+' · '+esc(i.volume_l)+' л · '+esc(i.qty)+' шт.</div></div></div>').join(''):'<p class="small">Нет позиций с одновременно известными габаритами и адресом. Маршрут не рассчитан.</p>';
  const stats=Object.entries(d.route_alternatives||{}).map(([mode,v])=>
    '<div class="metric">'+esc(({hybrid:'Габариты → адрес',size:'Крупные сначала',address:'Адреса по порядку'}[mode]||mode))+
    '<strong>'+v.line_switches+' линий / '+v.size_increases+' увеличений</strong></div>').join('');
  const sheet=d.picking_sheet;
  const rows=sheet.rows.map(i=>'<tr><td>'+i.step+'</td><td class="cell-code">'+esc(i.location)+
    '</td><td><b>'+esc(i.article)+'</b></td><td>'+i.qty+'</td><td>'+esc(i.dimensions_cm.join(' × '))+
    '</td><td>'+i.volume_l+'</td><td>□</td></tr>').join('');
  const omitted=sheet.excluded.length?
    '<div class="issues"><b>Не включены, требуется уточнение</b>'+
    sheet.excluded.map(i=>'<div>'+esc(i.article)+' · '+esc(i.reason)+'</div>').join('')+'</div>':'';
  const printed=sheet.rows.length?
    '<div id="printSheet"><div class="sheet-toolbar"><div><b class="sheet-demo">ТЕСТ · НЕ ДЛЯ РЕАЛЬНОЙ СБОРКИ</b>'+
    '<h2>Ozon FBO · Виртуальный сборочный лист</h2></div>'+
    '<button type="button" onclick="window.print()">Печать пробного листа</button></div>'+
    '<div class="pick-meta">Режим: '+esc(({hybrid:'Габаритная группа → адрес',size:'Крупные сначала',address:'Ячейки по порядку'}[sheet.mode]||sheet.mode))+
    (sheet.recommended_mode?' (выбран автоматически)':'')+' · Позиций: '+sheet.positions+' · Штук: '+sheet.units+
    ' · Адреса: карта GRIT · Размеры: Google Sheets · Дистанция не рассчитана</div>'+
    '<div style="overflow-x:auto"><table class="pick-sheet"><thead><tr><th>№</th><th>Ячейка</th>'+
    '<th>Артикул</th><th>Шт.</th><th>Габариты, см</th><th>Объём, л</th><th>✓</th></tr></thead>'+
    '<tbody>'+rows+'</tbody></table></div>'+omitted+'</div>':
    '<p class="small">Не удалось сформировать маршрут: для товаров нужны габариты и однозначные адреса из карты.</p>';
  report.innerHTML='<h2>Сравнение вариантов порядка</h2><div class="metrics">'+stats+'</div>'+
   '<p class="small">Это сравнение по числу переходов между линиями и увеличений объёма. Не физически кратчайший путь.</p>'+
   issues+'<h2 style="margin-top:16px">Предложенный маршрут · выбранный режим</h2>'+items+printed;
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
    try:
        # Backwards-compatible XLSX API for existing synthetic tests only.
        # Main UI uses direct, private Google Sheets access, no file upload.
        if upload:
            if not str(upload.filename or "").lower().endswith(".xlsx"):
                return jsonify(error="Файл должен быть XLSX"), 400
            catalog = read_dimensions_xlsx(upload.read(3 * 1024 * 1024 + 1))
        else:
            catalog, _synced_at, _cached = get_google_catalog()
        basket = parse_synthetic_basket(request.form.get("basket", ""))
        mode = request.form.get("mode", "hybrid")
        if mode not in ("auto", "hybrid", "size", "address"):
            raise ValueError("Некорректный алгоритм")
        located, issues = join_virtual_basket(catalog, basket, _map_assignments_for_preview())
        sheet = make_virtual_picking_sheet(located, issues, mode)
        selected_mode = sheet["mode"]
        route = plan_route(located, selected_mode) if located else None
        alternatives = {
            candidate: {"line_switches": result["line_switches"],
                        "size_increases": result["size_increases"]}
            for candidate, result in recommend_route(located)[1].items()
        } if located else {}
        return jsonify({
            "synthetic_basket": True, "read_only": True, "saved": False,
            "requested": len(basket), "matched": len(located),
            "catalog_complete": len(catalog["items"]),
            "catalog_incomplete": len(catalog["incomplete"]),
            "issues": issues, "route": route, "picking_sheet": sheet,
            "route_alternatives": alternatives,
        })
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    except GoogleSheetsSyncError as exc:
        return jsonify(error=str(exc)), 503
    except Exception:
        return jsonify(error="Не удалось прочитать карту склада для предпросмотра"), 503


@route_lab_bp.route("/api/manager/ozon-fbo/route-lab/google-sync", methods=["GET", "POST"])
def route_lab_google_sync():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    try:
        catalog, timestamp, cached = get_google_catalog(
            force=(request.method == "POST" and request.args.get("force") == "1"))
        return jsonify(connected=True, configured=True, read_only=True,
                       catalog_complete=len(catalog["items"]),
                       catalog_incomplete=len(catalog["incomplete"]),
                       synced_at=timestamp, cached=cached)
    except GoogleSheetsSyncError as exc:
        return jsonify(error=str(exc), configured=sync_configuration()["configured"],
                       connected=False), 503
