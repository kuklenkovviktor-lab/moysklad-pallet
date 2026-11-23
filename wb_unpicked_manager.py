"""Manager-only unpicked WB orders and original 58x40 WB sticker print.
No fulfillment mutations; no generated imitation of WB's barcode.
"""
import base64
import binascii
import hmac
import io
import json
import os
import secrets

from flask import Blueprint, Response, jsonify, request, session

from wms import manager_access, query_all, query_one
from wb_fbs_sync_v11 import WBClient

wb_unpicked_bp = Blueprint("wb_unpicked", __name__)
MAX_PRINT = 50
PAGE_SIZE = 50
GROUP_PAGE_SIZE = 50


def _account():
    return os.getenv("WB_FBS_V11_ACCOUNT_ID", "default").strip() or "default"


def _enabled():
    return os.getenv("WB_FBS_V11_ENABLED", "0") == "1"


def _orders_sql(extra=""):
    return """FROM wb_fbs_picking_queue q
      JOIN wb_fbs_orders o
        ON o.account_id=q.account_id AND o.order_id=q.order_id
      WHERE q.account_id=?
        AND o.local_status='pending'
        AND o.wb_status IN ('confirm','complete') """ + extra


def _read_page(account, status, search, page, article=None):
    terms = [account]
    condition = ""
    if status in ("confirm", "complete"):
        condition += " AND o.wb_status=?"
        terms.append(status)
    if article is not None:
        condition += " AND COALESCE(o.seller_article,'')=?"
        terms.append(article)
    if search:
        condition += """ AND (LOWER(o.nm_id) LIKE ? OR LOWER(o.seller_article) LIKE ?
            OR o.order_id LIKE ?
            OR EXISTS (SELECT 1 FROM wb_fbs_order_skus ss
              WHERE ss.account_id=o.account_id AND ss.order_id=o.order_id
                AND LOWER(ss.sku) LIKE ?))"""
        term = "%" + search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        # Escaped wildcards for predictable literal search across SQLite / PG.
        condition = condition.replace("LIKE ?", "LIKE ? ESCAPE '\\'")
        terms.extend([term, term, term, term])
    count = query_one("SELECT COUNT(*) " + _orders_sql(condition), tuple(terms))
    total = int(count[0] or 0)
    ids = query_all("""SELECT o.order_id """ + _orders_sql(condition) +
                    " ORDER BY q.accepted_at DESC,o.order_id DESC LIMIT ? OFFSET ?",
                    tuple(terms + [PAGE_SIZE, (page-1)*PAGE_SIZE]))
    order_ids = [str(r[0]) for r in ids]
    if not order_ids:
        return total, []
    marks = ",".join("?" for _ in order_ids)
    rows = query_all("""SELECT o.order_id,o.seller_article,o.wb_status,
           o.nm_id,o.chrt_id,s.sku,t.barcode
        FROM wb_fbs_orders o
        LEFT JOIN wb_fbs_order_skus s
          ON s.account_id=o.account_id AND s.order_id=o.order_id
        LEFT JOIN wb_fbs_stickers t
          ON t.account_id=o.account_id AND t.order_id=o.order_id
        WHERE o.account_id=? AND o.order_id IN (""" + marks + ")",
        tuple([account] + order_ids))
    output = {oid: {"order_id": oid, "seller_article": "", "wb_status": "",
                    "nm_id": "", "chrt_id": "", "product_barcodes": [],
                    "sticker_available": False} for oid in order_ids}
    for oid, article, state, nm, chrt, sku, barcode in rows:
        item = output[str(oid)]
        item.update(seller_article=article or "", wb_status=state,
                    nm_id=nm or "", chrt_id=chrt or "",
                    sticker_available=bool(barcode))
        if sku and sku not in item["product_barcodes"]:
            item["product_barcodes"].append(sku)
    return total, [output[oid] for oid in order_ids]


@wb_unpicked_bp.get("/manager/wb-fbs/unpicked")
def unpicked_page():
    if not manager_access():
        return Response("Доступ только руководителю", status=403)
    token = secrets.token_urlsafe(32)
    session["wb_unpicked_print_csrf"] = token
    html = PAGE.replace("__CSRF_JSON__", json.dumps(token))
    response = Response(html, mimetype="text/html")
    response.headers["Cache-Control"] = "no-store"
    return response


@wb_unpicked_bp.get("/api/manager/wb-fbs/unpicked")
def unpicked_list():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not _enabled():
        return jsonify(error="WB FBS отключён"), 503
    search = (request.args.get("q") or "").strip()[:80]
    status = request.args.get("status", "all")
    if status not in ("all", "confirm", "complete"):
        return jsonify(error="Неверный фильтр"), 400
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return jsonify(error="Неверная страница"), 400
    if page < 1 or page > 10000:
        return jsonify(error="Неверная страница"), 400
    article = request.args.get("article")
    if article is not None and len(article) > 180:
        return jsonify(error="Слишком длинный артикул"), 400
    total, rows = _read_page(_account(), status, search, page, article)
    response = jsonify(total=total, page=page, page_size=PAGE_SIZE, rows=rows)
    response.headers["Cache-Control"] = "no-store"
    return response



def _read_groups(account, status, search, page):
    """Group across the entire admitted, pending WB queue, not one order page."""
    terms = [account]
    condition = ""
    if status in ("confirm", "complete"):
        condition += " AND o.wb_status=?"
        terms.append(status)
    if search:
        condition += """ AND (LOWER(o.nm_id) LIKE ? ESCAPE '\'
          OR LOWER(o.seller_article) LIKE ? ESCAPE '\'
          OR o.order_id LIKE ? ESCAPE '\'
          OR EXISTS (SELECT 1 FROM wb_fbs_order_skus ss
             WHERE ss.account_id=o.account_id AND ss.order_id=o.order_id
               AND LOWER(ss.sku) LIKE ? ESCAPE '\'))"""
        term = "%" + search.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        terms.extend([term] * 4)
    base = _orders_sql(condition)
    count = query_one("SELECT COUNT(DISTINCT COALESCE(o.seller_article,'')) " + base, tuple(terms))
    total_groups = int((count or (0,))[0] or 0)
    orders = query_one("SELECT COUNT(*) " + base, tuple(terms))
    total_orders = int((orders or (0,))[0] or 0)
    rows = query_all("""SELECT COALESCE(o.seller_article,'') AS article,
          COUNT(*) AS order_count,
          SUM(CASE WHEN o.wb_status='confirm' THEN 1 ELSE 0 END) AS confirm_count,
          SUM(CASE WHEN o.wb_status='complete' THEN 1 ELSE 0 END) AS delivery_count
          """ + base + """
          GROUP BY COALESCE(o.seller_article,'')
          ORDER BY COUNT(*) DESC,COALESCE(o.seller_article,'') ASC
          LIMIT ? OFFSET ?""",
          tuple(terms + [GROUP_PAGE_SIZE, (page-1)*GROUP_PAGE_SIZE]))
    return total_groups, total_orders, [
        dict(article=r[0], count=int(r[1]), confirm=int(r[2] or 0), delivery=int(r[3] or 0))
        for r in rows
    ]


@wb_unpicked_bp.get("/api/manager/wb-fbs/unpicked/groups")
def unpicked_groups():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not _enabled():
        return jsonify(error="WB FBS отключён"), 503
    search = (request.args.get("q") or "").strip()[:80]
    status = request.args.get("status", "all")
    if status not in ("all", "confirm", "complete"):
        return jsonify(error="Неверный фильтр"), 400
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        return jsonify(error="Неверная страница"), 400
    if not 1 <= page <= 10000:
        return jsonify(error="Неверная страница"), 400
    total, orders, groups = _read_groups(_account(), status, search, page)
    response = jsonify(total=total, orders=orders, page=page,
                       page_size=GROUP_PAGE_SIZE, groups=groups)
    response.headers["Cache-Control"] = "no-store"
    return response


@wb_unpicked_bp.post("/api/manager/wb-fbs/unpicked/print")
def unpicked_print():
    if not manager_access():
        return jsonify(error="Доступ только руководителю"), 403
    if not _enabled():
        return jsonify(error="WB FBS отключён"), 503
    token = request.headers.get("X-GRIT-CSRF", "")
    expected = session.get("wb_unpicked_print_csrf", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        return jsonify(error="Обнови страницу и повтори печать"), 403
    # Flask's same-site cookies are an additional defense, not a CSRF substitute.
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Ожидается JSON"), 400
    raw = data.get("order_ids")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_PRINT:
        return jsonify(error="Выбери от 1 до 50 заказов"), 400
    ids = [str(x) for x in raw]
    if len(set(ids)) != len(ids) or any(not x.isdecimal() or len(x) > 20 for x in ids):
        return jsonify(error="Некорректные номера заказов"), 400
    account = _account()
    marks = ",".join("?" for _ in ids)
    valid = query_all("""SELECT o.order_id
         FROM wb_fbs_orders o
         JOIN wb_fbs_picking_queue q
           ON q.account_id=o.account_id AND q.order_id=o.order_id
         WHERE o.account_id=? AND o.order_id IN (""" + marks + """)
           AND o.local_status='pending'
           AND o.wb_status IN ('confirm','complete')""", tuple([account] + ids))
    if {str(x[0]) for x in valid} != set(ids):
        return jsonify(error="Очередь изменилась. Обнови список перед печатью"), 409

    api_token = os.getenv("WB_API_TOKEN", "").strip()
    if not api_token:
        return jsonify(error="Не настроен ключ WB для получения стикеров"), 503
    try:
        result = WBClient(api_token).stickers(ids)
    except Exception:
        return jsonify(error="WB не отдал оригинальные стикеры. Повтори позже"), 502

    stickers = {}
    for item in result:
        if not isinstance(item, dict):
            continue
        oid = str(item.get("orderId", item.get("order_id", "")))
        if oid not in ids or oid in stickers:
            continue
        encoded = item.get("file")
        if not isinstance(encoded, str) or len(encoded) > 3000000:
            continue
        try:
            png = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) < 2200000:
            stickers[oid] = png
    if set(stickers) != set(ids):
        return jsonify(error="WB вернул не все оригинальные PNG. Печать отменена, без пропусков"), 502

    import fitz
    pdf = fitz.open()
    width, height = 58 * 72 / 25.4, 40 * 72 / 25.4
    try:
        for oid in ids:
            page = pdf.new_page(width=width, height=height)
            # Exact WB bitmap (no re-created codes); no margins or cropping.
            page.insert_image(page.rect, stream=stickers[oid], keep_proportion=True)
        result_bytes = pdf.tobytes(garbage=4, deflate=True)
    except Exception:
        return jsonify(error="Не удалось сформировать PDF стикеров WB"), 500
    finally:
        pdf.close()
    response = Response(result_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = 'inline; filename="grit-wb-unpicked-58x40.pdf"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#09121e">
<title>GRIT WMS · Несобранные WB</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#09121e;color:#f1f6ff;font:14px system-ui,Arial,sans-serif}
main{max-width:1400px;margin:auto;padding:20px}
a{color:#81c9ff}h1{margin:10px 0 6px;font-size:25px}
.muted{color:#9eb5ca}.panel{background:#142337;border:1px solid #30465c;border-radius:12px;padding:17px;margin-top:15px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input,select{background:#0d1d2c;color:#f1f6ff;border:1px solid #42607a;padding:11px;border-radius:9px;font:inherit}
input[type=search]{min-width:230px;flex:1}
input[type=checkbox]{accent-color:#2488cf;width:17px;height:17px}
button{background:#176eae;border:1px solid #358acc;border-radius:9px;color:white;padding:10px 14px;font:inherit;font-weight:650;cursor:pointer}
button:hover{background:#2488cf}button:disabled{opacity:.45;cursor:not-allowed}
button.secondary{background:#233b50;border-color:#375a75}
:focus-visible{outline:2px solid #ff9230;outline-offset:2px}
table{width:100%;border-collapse:collapse}th,td{padding:12px 9px;border-bottom:1px solid #30465c;text-align:left;vertical-align:middle}
th{color:#adc4d7;font-size:12px}tr:hover td{background:#1b3147}
.code{font-family:ui-monospace,monospace;font-size:12px;overflow-wrap:anywhere}
.status{color:#ffcc8b}.ok{color:#87dcba}.error{color:#ff9b9b}
.hint{margin:11px 0;color:#9eb5ca;font-size:12px}
.pages{display:flex;gap:10px;align-items:center;margin-top:15px}
.article-btn{background:transparent!important;border:0!important;color:#80cbff;padding:5px 0;text-align:left}
.article-btn:hover{text-decoration:underline;background:transparent!important}
.group-count{font-size:17px;font-weight:750}
.toolbar label{display:inline-flex;gap:7px;align-items:center}
#detailPanel{display:none}
@media(max-width:760px){main{padding:12px}.scroll{overflow-x:auto}table{min-width:820px}}
</style></head><body><main>
<a href="/manager">← Вернуться на главную</a>
<h1>Несобранные заказы WB</h1>
<div class="muted">Товары сгруппированы по артикулу продавца по всей очереди GRIT. Внутри группы можно печатать оригинальные WB-стикеры 58×40 мм по заказам.</div>
<div class="panel">
<div class="toolbar">
<input id="query" type="search" placeholder="Артикул WB, артикул продавца, ШК или заказ" aria-label="Поиск">
<select id="status" aria-label="Статус WB">
<option value="all">Все статусы</option><option value="confirm">На сборке</option><option value="complete">В доставке WB</option>
</select>
<button id="refresh" class="secondary">Обновить</button>
</div>
<p id="summary" class="hint">Загрузка…</p>
<section id="groupsPanel" aria-label="Группы товаров">
<div class="scroll"><table><thead><tr><th>Артикул продавца</th><th>Заказов</th><th>На сборке</th><th>В доставке</th><th>Действие</th></tr></thead>
<tbody id="groups"><tr><td colspan="5">Загрузка…</td></tr></tbody></table></div>
<div class="pages"><button class="secondary" id="gprev">← Назад</button><span id="gpage">1</span><button class="secondary" id="gnext">Далее →</button></div>
</section>
<section id="detailPanel" aria-label="Заказы выбранного артикула">
<div class="toolbar">
<button id="back" class="secondary">← К артикулам</button>
<strong id="articleTitle"></strong>
<span class="muted" id="articleSummary"></span>
</div>
<div class="toolbar" style="margin-top:14px">
<label><input id="selectAll" type="checkbox"> Выбрать все заказы этой страницы (до 50)</label>
<button id="print" disabled>Печать WB 58×40 (0)</button>
<span class="muted" id="message" role="status"></span>
</div>
<div class="scroll"><table><thead><tr><th></th><th>Артикул WB (nmID)</th><th>Артикул продавца</th><th>ШК товара</th><th>Заказ WB</th><th>Статус</th><th>Стикер</th></tr></thead>
<tbody id="rows"><tr><td colspan="7">Выбери артикул</td></tr></tbody></table></div>
<div class="pages"><button class="secondary" id="prev">← Предыдущие 50</button><span id="page">1</span><button class="secondary" id="next">Следующие 50 →</button></div>
<p class="hint">Если заказов больше 50, распечатай страницу и перейди к следующей. Печать не закрывает заказы и не меняет статусы WB.</p>
</section>
<p class="hint">Нет артикула? Такие заказы попадают в группу «Без артикула». Список не включает собранные, отменённые и не принятые GRIT заказы.</p>
</div></main><script>
const csrf=__CSRF_JSON__;
let groupPage=1,detailPage=1,groupsTotal=0,total=0,rows=[],selected=new Set(),busy=false,serial=0,article=null;
const $=x=>document.getElementById(x);
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function queryParams(page){return new URLSearchParams({page:String(page),status:$('status').value,q:$('query').value.trim()})}
function updateButtons(){
 $('print').disabled=busy||selected.size===0||selected.size>50;
 $('print').textContent='Печать WB 58×40 ('+selected.size+')';
 $('selectAll').checked=rows.length>0&&rows.every(x=>selected.has(x.order_id));
 $('selectAll').indeterminate=rows.some(x=>selected.has(x.order_id))&&!$('selectAll').checked;
}
async function loadGroups(){
 const call=++serial;article=null;rows=[];selected.clear();updateButtons();
 $('groupsPanel').style.display='';$('detailPanel').style.display='none';
 $('groups').innerHTML='<tr><td colspan="5">Загрузка…</td></tr>';
 try{
  const r=await fetch('/api/manager/wb-fbs/unpicked/groups?'+queryParams(groupPage),{cache:'no-store'});
  const d=await r.json();if(call!==serial)return;
  if(!r.ok)throw Error(d.error||'Ошибка загрузки групп');
  groupsTotal=d.total||0;
  $('summary').textContent='Несобранных заказов: '+d.orders+' · Артикулов: '+groupsTotal;
  $('gpage').textContent=groupPage+' / '+Math.max(1,Math.ceil(groupsTotal/50));
  $('gprev').disabled=groupPage===1;$('gnext').disabled=groupPage*50>=groupsTotal;
  $('groups').innerHTML=d.groups.length?d.groups.map((g,i)=>'<tr><td><button class="article-btn open-group" data-index="'+i+'">'+esc(g.article||'Без артикула')+'</button></td><td class="group-count">'+g.count+'</td><td class="ok">'+g.confirm+'</td><td class="status">'+g.delivery+'</td><td><button class="secondary open-group" data-index="'+i+'">Открыть заказы →</button></td></tr>').join('')
    :'<tr><td colspan="5">По фильтру несобранных заказов нет</td></tr>';
  window.groupData=d.groups;
 }catch(e){if(call===serial)$('groups').innerHTML='<tr><td colspan="5" class="error">'+esc(e.message)+'</td></tr>';}
}
async function loadOrders(){
 if(article===null)return;
 const call=++serial;selected.clear();updateButtons();
 $('rows').innerHTML='<tr><td colspan="7">Загрузка…</td></tr>';
 const params=queryParams(detailPage);params.set('article',article);
 try{
  const r=await fetch('/api/manager/wb-fbs/unpicked?'+params,{cache:'no-store'});
  const d=await r.json();if(call!==serial)return;
  if(!r.ok)throw Error(d.error||'Ошибка загрузки заказов');
  rows=d.rows||[];total=d.total||0;
  $('articleSummary').textContent='Несобранных: '+total+' · на странице: '+rows.length;
  $('page').textContent=detailPage+' / '+Math.max(1,Math.ceil(total/50));
  $('prev').disabled=detailPage===1;$('next').disabled=detailPage*50>=total;
  $('rows').innerHTML=rows.length?rows.map(x=>'<tr><td><input type="checkbox" class="pick" data-id="'+esc(x.order_id)+'" aria-label="Выбрать заказ '+esc(x.order_id)+'"></td><td><b>'+esc(x.nm_id||'—')+'</b></td><td class="code">'+esc(x.seller_article||'—')+'</td><td class="code">'+esc(x.product_barcodes.join(', ')||'Нет ШК')+'</td><td class="code">'+esc(x.order_id)+'</td><td class="'+(x.wb_status==='complete'?'status':'ok')+'">'+(x.wb_status==='complete'?'В доставке':'На сборке')+'</td><td>'+(x.sticker_available?'Код получен':'Запросим у WB')+'</td></tr>').join('')
   :'<tr><td colspan="7">Заказы закончились или очередь изменилась</td></tr>';
  $('message').textContent='';
 }catch(e){if(call===serial){$('rows').innerHTML='<tr><td colspan="7" class="error">'+esc(e.message)+'</td></tr>';$('articleSummary').textContent='Ошибка';}}
 updateButtons();
}
$('groups').addEventListener('click',e=>{
 const target=e.target.closest('.open-group');if(!target||!window.groupData)return;
 const group=window.groupData[Number(target.dataset.index)];if(!group)return;
 article=group.article;detailPage=1;selected.clear();
 $('articleTitle').textContent=article||'Без артикула';
 $('groupsPanel').style.display='none';$('detailPanel').style.display='';loadOrders();
});
$('back').addEventListener('click',()=>{loadGroups()});
$('rows').addEventListener('change',e=>{
 if(!e.target.classList.contains('pick'))return;
 const id=e.target.dataset.id;
 if(e.target.checked){if(selected.size>=50){e.target.checked=false;$('message').textContent='За раз не более 50 стикеров';return;}selected.add(id)}
 else selected.delete(id);
 updateButtons();
});
$('selectAll').addEventListener('change',e=>{
 if(e.target.checked)rows.forEach(x=>selected.add(x.order_id));else selected.clear();
 $('rows').querySelectorAll('.pick').forEach(x=>x.checked=selected.has(x.dataset.id));updateButtons();
});
$('print').addEventListener('click',async()=>{
 if(busy||!selected.size||selected.size>50)return;
 const ids=rows.filter(x=>selected.has(x.order_id)).map(x=>x.order_id);
 const preview=window.open('','_blank');
 busy=true;updateButtons();$('message').textContent='Получаем оригинальные стикеры WB…';
 try{
  const r=await fetch('/api/manager/wb-fbs/unpicked/print',{method:'POST',headers:{'Content-Type':'application/json','X-GRIT-CSRF':csrf},body:JSON.stringify({order_ids:ids})});
  if(!r.ok){const d=await r.json();throw Error(d.error||'Ошибка WB '+r.status);}
  const url=URL.createObjectURL(await r.blob());
  if(preview)preview.location.href=url;
  else{const a=document.createElement('a');a.href=url;a.download='grit-wb-58x40.pdf';document.body.appendChild(a);a.click();a.remove();}
  $('message').textContent='PDF создан. Выбери Xprinter с этикеткой 58×40 мм.';
  setTimeout(()=>URL.revokeObjectURL(url),120000);
 }catch(e){if(preview)preview.close();$('message').textContent=e.message;}
 finally{busy=false;updateButtons();}
});
let timer;
$('query').addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{groupPage=1;loadGroups()},350)});
$('status').addEventListener('change',()=>{groupPage=1;loadGroups()});
$('refresh').addEventListener('click',()=>{if(article!==null)loadOrders();else loadGroups()});
$('gprev').addEventListener('click',()=>{if(groupPage>1){groupPage--;loadGroups()}});
$('gnext').addEventListener('click',()=>{if(groupPage*50<groupsTotal){groupPage++;loadGroups()}});
$('prev').addEventListener('click',()=>{if(detailPage>1){detailPage--;loadOrders()}});
$('next').addEventListener('click',()=>{if(detailPage*50<total){detailPage++;loadOrders()}});
loadGroups();
</script></body></html>"""
