"""Manager's mobile aisle walk: bind a confirmed product to an existing physical ONIX cell.

A barcode is not an address: addresses can be chosen on the map or entered as text.
Product names are verified read-only against MoySklad; this module changes ONIX mapping only.
"""
import threading
import uuid

from flask import Blueprint, jsonify, redirect, request

from wms import db_connect, ensure_db, manager_access, now_iso, query_all, query_one, sql
from replenishment_checks import _scan_code

cell_audit_bp = Blueprint("cell_audit", __name__)
_INIT_LOCK = threading.Lock()
_INIT_DONE = False


def ensure_cell_audit_db():
    global _INIT_DONE
    ensure_db()
    if _INIT_DONE:
        return
    with _INIT_LOCK:
        if _INIT_DONE:
            return
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql("""
                    CREATE TABLE IF NOT EXISTS cell_sku_assignments (
                        location_id TEXT PRIMARY KEY,
                        product_id TEXT NOT NULL,
                        article TEXT NOT NULL,
                        product_name TEXT NOT NULL DEFAULT '',
                        source_barcode TEXT NOT NULL DEFAULT '',
                        assigned_at TEXT NOT NULL,
                        assigned_by TEXT NOT NULL
                    )
                """))
                cur.execute(sql("""
                    CREATE TABLE IF NOT EXISTS cell_sku_assignment_events (
                        id TEXT PRIMARY KEY,
                        location_id TEXT NOT NULL,
                        old_product_id TEXT,
                        old_article TEXT,
                        new_product_id TEXT,
                        new_article TEXT,
                        kind TEXT NOT NULL,
                        actor TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                """))
                conn.commit()
            finally:
                cur.close()
        _INIT_DONE = True


def _locate(code):
    code = _scan_code(code)
    if not code or len(code) > 160:
        return None
    row = query_one("""
        SELECT id, label, barcode FROM replenishment_locations
        WHERE active = 1 AND (id = ? OR label = ? OR barcode = ?)
        LIMIT 1
    """, (code, code.upper(), code))
    if not row:
        return None
    return {"id": row[0], "label": row[1], "barcode": row[2]}


def _assignment(location_id):
    row = query_one("""
        SELECT product_id, article, product_name, source_barcode, assigned_at, assigned_by
        FROM cell_sku_assignments WHERE location_id = ?
    """, (location_id,))
    return ({"product_id":row[0],"article":row[1],"name":row[2],
             "source_barcode":row[3],"assigned_at":row[4],"assigned_by":row[5]}
            if row else None)


def _product_from_scan(body):
    """Read-only catalogue lookup. Explicit exact article fallback, never fuzzy binding."""
    barcode = _scan_code(body.get("barcode"))
    article = str(body.get("article") or "").strip()
    if bool(barcode) == bool(article):
        return None, "Укажи либо штрихкод товара, либо точный артикул", 400
    if barcode:
        if len(barcode) > 160 or len(barcode) < 3 or barcode.startswith("ONIX-LOC-"):
            return None, "Нужен штрихкод товара, а не ячейки", 400
        try:
            from app import find_product_by_any_barcode
            product, _pack = find_product_by_any_barcode(barcode)
        except Exception:
            return None, "Каталог МойСклад недоступен, попробуй позже", 502
    else:
        if not 2 <= len(article) <= 120:
            return None, "Введи точный артикул с этикетки", 400
        try:
            from app import _get_product_list
            matching = [p for p in _get_product_list()
                        if str(p.get("article") or "").strip().casefold() == article.casefold()
                        and p.get("id")]
        except Exception:
            return None, "Каталог МойСклад недоступен, попробуй позже", 502
        if len(matching) > 1:
            return None, "Найдено несколько товаров с этим артикулом. Используй ШК товара.", 409
        product = matching[0] if matching else None
    if not product or not product.get("id") or not product.get("article"):
        return None, "Товар не найден в МойСклад. Привязка не записана.", 404
    return {
        "id": str(product["id"]),
        "article": str(product["article"]).strip(),
        "name": str(product.get("name") or "").strip(),
        "source": barcode or "",
        "method": "barcode" if barcode else "article",
    }, None, 200


@cell_audit_bp.get("/manager/cell-audit")
def cell_audit_page():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    return CELL_AUDIT_PAGE


@cell_audit_bp.get("/api/manager/cell-audit/locate")
def cell_audit_locate():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_cell_audit_db()
    location = _locate(request.args.get("code"))
    if not location:
        return jsonify(error="Ячейка не зарегистрирована. Добавь её на карте склада."), 404
    return jsonify(location=location, assigned=_assignment(location["id"]))


@cell_audit_bp.post("/api/manager/cell-audit/preview")
def cell_audit_preview():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_cell_audit_db()
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        return jsonify(error="Нужны данные сканирования"), 400
    location = _locate(data.get("location"))
    if not location:
        return jsonify(error="Неизвестная ячейка. Выбери её на карте или введи адрес."), 404
    product, error, status = _product_from_scan(data)
    if error:
        return jsonify(error=error), status
    return jsonify(location=location, product=product,
                   previous=_assignment(location["id"]), stock_changed=False)


@cell_audit_bp.post("/api/manager/cell-audit/assign")
def cell_audit_assign():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_cell_audit_db()
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict) or "expected_current_id" not in data:
        return jsonify(error="Сначала проверь товар перед записью"), 400
    location = _locate(data.get("location"))
    if not location:
        return jsonify(error="Ячейка не существует, запись не выполнена"), 404
    product, error, status = _product_from_scan(data)
    if error:
        return jsonify(error=error), status
    if str(data.get("confirmed_product_id") or "") != product["id"]:
        return jsonify(error="Артикул изменился после проверки. Проверь его заново."), 409
    expected = data.get("expected_current_id")
    if expected is not None and (not isinstance(expected, str) or not expected):
        return jsonify(error="Некорректное подтверждение предыдущего товара"), 400
    allow_replace = data.get("replace") is True
    previous = None
    stamp = now_iso()
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                # PostgreSQL locks the existing mapping to prevent lost updates.
                statement = "SELECT product_id, article FROM cell_sku_assignments WHERE location_id = ?"
                from wms import DATABASE_URL
                if DATABASE_URL:
                    statement += " FOR UPDATE"
                cur.execute(sql(statement), (location["id"],))
                row = cur.fetchone()
                previous = {"product_id":row[0], "article":row[1]} if row else None
                actual = previous["product_id"] if previous else None
                if actual != expected:
                    return jsonify(error="Привязка ячейки уже изменилась. Обнови карточку.",
                                   current=previous), 409
                if actual == product["id"]:
                    return jsonify(ok=True, unchanged=True, location=location,
                                   article=product["article"], stock_changed=False)
                if previous and not allow_replace:
                    return jsonify(error="Ячейка уже закреплена за другим товаром. Требуется подтверждение замены.",
                                   current=previous), 409
                if previous:
                    cur.execute(sql("""
                        UPDATE cell_sku_assignments
                        SET product_id=?, article=?, product_name=?, source_barcode=?,
                            assigned_at=?, assigned_by=? WHERE location_id=? AND product_id=?
                    """), (product["id"], product["article"], product["name"], product["source"],
                           stamp, "Руководитель", location["id"], actual))
                    if cur.rowcount != 1:
                        return jsonify(error="Привязка изменилась. Проверь повторно."), 409
                else:
                    cur.execute(sql("""
                        INSERT INTO cell_sku_assignments
                          (location_id, product_id, article, product_name, source_barcode, assigned_at, assigned_by)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """), (location["id"], product["id"], product["article"], product["name"],
                           product["source"], stamp, "Руководитель"))
                cur.execute(sql("""
                    INSERT INTO cell_sku_assignment_events
                    (id, location_id, old_product_id, old_article,
                     new_product_id, new_article, kind, actor, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """), (str(uuid.uuid4()), location["id"], actual,
                       previous["article"] if previous else None, product["id"], product["article"],
                       "replace" if previous else "assign", "Руководитель", stamp))
                conn.commit()
            finally:
                cur.close()
    except Exception:
        # Transaction is rolled back by db_connect context manager if needed.
        return jsonify(error="Не удалось сохранить. Проверь текущую привязку и повтори после сверки."), 409
    return jsonify(ok=True, unchanged=False, location=location,
                   article=product["article"], replaced=bool(previous), stock_changed=False)



@cell_audit_bp.post("/api/manager/cell-audit/move")
def cell_audit_move():
    """Manager-only atomic reassignment to an already registered, empty cell.

    This changes GRIT address mapping only, never MoySklad stocks or locations.
    Explicit expected_product_id prevents stale browser overwrites.
    """
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_cell_audit_db()
    data = request.get_json(silent=True) if request.is_json else None
    if not isinstance(data, dict):
        return jsonify(error="Нужны данные переноса"), 400
    source = _locate(data.get("source"))
    target = _locate(data.get("target"))
    if not source or not target:
        return jsonify(error="Одна из ячеек не зарегистрирована на карте склада"), 404
    if source["id"] == target["id"]:
        return jsonify(error="Выбери другую ячейку для переноса"), 400
    expected = str(data.get("expected_product_id") or "").strip()
    if not expected or len(expected) > 160:
        return jsonify(error="Сначала найди исходную ячейку с привязанным товаром"), 400
    stamp = now_iso()
    try:
        with db_connect() as conn:
            cur = conn.cursor()
            try:
                from wms import DATABASE_URL
                # Order locks consistently across simultaneous moves to avoid
                # deadlocks; an occupied destination must never be overwritten.
                query = ("SELECT location_id,product_id,article,product_name,source_barcode "
                         "FROM cell_sku_assignments WHERE location_id IN (?,?)")
                if DATABASE_URL:
                    query += " ORDER BY location_id FOR UPDATE"
                else:
                    query += " ORDER BY location_id"
                cur.execute(sql(query), (source["id"], target["id"]))
                assignments = {row[0]: row for row in cur.fetchall()}
                original = assignments.get(source["id"])
                destination = assignments.get(target["id"])
                if not original or original[1] != expected:
                    return jsonify(error="Исходная привязка изменилась. Найди ячейку заново."), 409
                if destination:
                    return jsonify(error="Новая ячейка уже занята: " + destination[2] +
                                   ". Перенос отменён, привязки не изменены."), 409

                # Delete exactly the confirmed source, then insert its exact
                # product data into the target in a single transaction.
                cur.execute(sql(
                    "DELETE FROM cell_sku_assignments WHERE location_id=? AND product_id=?"
                ), (source["id"], expected))
                if cur.rowcount != 1:
                    return jsonify(error="Привязка изменилась. Повтори проверку."), 409
                cur.execute(sql("""
                    INSERT INTO cell_sku_assignments
                    (location_id,product_id,article,product_name,source_barcode,assigned_at,assigned_by)
                    VALUES (?,?,?,?,?,?,?)
                """), (target["id"], original[1], original[2], original[3],
                       original[4], stamp, "Руководитель"))
                for location_id, old_id, old_article, new_id, new_article, kind in (
                    (source["id"], original[1], original[2], None, None, "move_out"),
                    (target["id"], None, None, original[1], original[2], "move_in"),
                ):
                    cur.execute(sql("""
                        INSERT INTO cell_sku_assignment_events
                        (id,location_id,old_product_id,old_article,
                         new_product_id,new_article,kind,actor,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?)
                    """), (str(uuid.uuid4()), location_id, old_id, old_article,
                           new_id, new_article, kind, "Руководитель", stamp))
                conn.commit()
            finally:
                cur.close()
    except Exception:
        return jsonify(error="Не удалось перенести товар. Проверь обе ячейки и повтори."), 409
    return jsonify(ok=True, source=source, target=target, article=original[2],
                   stock_changed=False)


@cell_audit_bp.get("/api/manager/cell-audit/assignments")
def cell_audit_assignments():
    if not manager_access():
        return jsonify(error="Требуется вход руководителя"), 403
    ensure_cell_audit_db()
    rows = query_all("""
        SELECT a.location_id, l.label, a.article, a.product_name, a.assigned_at
        FROM cell_sku_assignments a
        JOIN replenishment_locations l ON l.id=a.location_id AND l.active=1
        ORDER BY l.label LIMIT 5000
    """)
    return jsonify(assignments=[
        {"location_id":r[0],"label":r[1],"article":r[2],"name":r[3],"assigned_at":r[4]}
        for r in rows
    ])


CELL_AUDIT_PAGE = r"""<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>GRIT WMS · Обход ячеек</title>
<script src="https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js"></script>
<style>
*{box-sizing:border-box}body{margin:0;font:16px system-ui,-apple-system,Segoe UI,sans-serif;color:#23394a;background:#e8eef3}
main{max-width:620px;margin:0 auto;padding:14px}
a{color:#355e77;text-decoration:none}
.card{background:#fff;border:1px solid #cbd7e2;border-radius:14px;padding:16px;margin:12px 0}
h1{font-size:23px;margin:12px 0}h2{font-size:18px;margin:8px 0 12px}
label{display:block;font-weight:650;font-size:13px;margin:10px 0 5px}
input{width:100%;padding:13px;border:1px solid #b6c7d4;border-radius:9px;font:inherit;background:#fff}
button{border:0;border-radius:10px;padding:13px;background:#40627b;color:#fff;font:inherit;font-weight:650;cursor:pointer;min-height:45px}
button.secondary{background:#dce8ef;color:#2a495f}
button:disabled{opacity:.55;cursor:not-allowed}
.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:9px}
.help{font-size:13px;color:#667989;line-height:1.5}
.info{border-radius:10px;background:#f0f5f8;padding:12px;overflow-wrap:anywhere}
.warning{background:#fff0d0;padding:11px;border-radius:8px;color:#7f550a;font-size:13px}
.bad{color:#a92929;font-weight:650}.good{color:#267348;font-weight:650}
#reader{margin:9px 0;overflow:hidden;max-width:100%}
#details{min-height:36px}.muted{color:#697e8e}.hidden{display:none!important}
.pill{display:inline-block;font-size:12px;border-radius:7px;padding:4px 8px;background:#e0edf4}
</style></head><body><main>
<a href="/manager/warehouse-map">← Карта склада</a>
<h1>📷 Обход ячеек</h1>
<p class="help">Без Excel: выбери адрес на карте или введи его, отсканируй товар и проверь карточку. ONIX закрепит артикул за ячейкой. Остатки МойСклад не меняются.</p>
<section class="card">
 <h2>1. Адрес ячейки</h2>
 <label for="location">Код ячейки / ШК ONIX</label>
 <input id="location" placeholder="К-07 или ONIX-LOC-..." autocomplete="off" autocapitalize="characters">
 <div class="actions"><button class="secondary" id="checkLocation" type="button">Найти ячейку</button>
 <button class="secondary" id="scanLocation" type="button">📷 Скан ячейки</button></div>
 <div id="locResult" class="help" aria-live="polite">Если на полке только текст артикула, выбери ячейку на карте или введи её адрес вручную.</div>
 <div id="movePanel" class="info hidden" style="margin-top:12px">
  <b>Изменить место закреплённого товара</b>
  <p id="moveItem" class="help"></p>
  <label for="moveTarget">Новая ячейка / ШК ONIX</label>
  <input id="moveTarget" autocomplete="off" placeholder="Например, K-08">
  <button id="moveButton" type="button" style="width:100%;margin-top:9px">Перенести привязку</button>
  <p class="help">Только руководитель. Новая ячейка должна быть пустой. Остатки МойСклад не меняются.</p>
 </div>
</section>
<section class="card">
 <h2>2. Какой товар лежит здесь?</h2>
 <label for="barcode">ШК товара (с упаковки)</label>
 <input id="barcode" placeholder="Сканируй товар или введи EAN/Code128" autocomplete="off" autocapitalize="off">
 <div class="actions"><button class="secondary" id="scanProduct" type="button">📷 Скан товара</button>
 <button class="secondary" id="preview" type="button">Проверить ШК</button></div>
 <details><summary class="help">На товаре нет ШК? Ввести артикул с полки</summary>
  <label for="article">Точный артикул МойСклад</label>
  <input id="article" placeholder="SALAT_3V1_NONE" autocomplete="off" autocapitalize="characters">
  <p class="help">При вводе артикула поле ШК должно быть пустым. ONIX проверит точное совпадение в МойСклад.</p>
 </details>
 <div id="reader"></div><div id="cameraHelp" class="help"></div>
</section>
<section class="card">
 <h2>3. Подтвердить привязку</h2>
 <div id="details" class="help">Сначала укажи ячейку и товар.</div>
 <div id="replaceWarning" class="warning hidden">ВНИМАНИЕ: за этой ячейкой уже закреплён другой артикул. Сверь товар физически до замены.</div>
 <button type="button" id="save" disabled style="width:100%;margin-top:12px">Закрепить товар за ячейкой</button>
 <button type="button" id="next" class="secondary" style="width:100%;margin-top:8px">Следующая ячейка</button>
 <p id="msg" class="help" role="status"></p>
</section>
<p class="help">Если на ячейке нет машиночитаемого ШК, камерой обычный текст надёжно не сканировать. После первичного обхода можно распечатать QR/Code128 ONIX для всех адресов.</p>
</main><script>
const el=(id)=>document.getElementById(id);
const escapeHTML=(s)=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let locationData=null,previewData=null,scanner=null,scanBusy=false,saving=false;
const msg=(s,cls='help')=>{el('msg').className=cls;el('msg').textContent=s};
async function api(url,options){
 const r=await fetch(url,options);let d;
 try{d=await r.json()}catch(_e){throw Error('Сервер ответил не JSON. Проверь подключение.')}
 if(!r.ok)throw Error(d.error||'Ошибка ONIX');return d;
}
function clearPreview(){
 previewData=null;el('save').disabled=true;el('replaceWarning').classList.add('hidden');
 el('details').textContent='Выбери адрес и проверь товар.';
}
function drawLocation(d){
 locationData=d;el('location').value=d.location.label;
 el('locResult').innerHTML='Найдена: <b>'+escapeHTML(d.location.label)+'</b>'+(d.assigned?
 ' · сейчас закреплено <b>'+escapeHTML(d.assigned.article)+'</b>':
 ' · ещё нет привязанного товара');
 clearPreview();
 el('movePanel').classList.toggle('hidden', !d.assigned);
 el('moveItem').textContent=d.assigned?'Закреплено: '+d.assigned.article+' → '+d.location.label:'';
 el('moveTarget').value='';
}
async function locate(){
 if(scanBusy)return;clearPreview();locationData=null;
 el('movePanel').classList.add('hidden');
 const code=el('location').value.trim();
 if(!code){el('locResult').textContent='Укажи адрес ячейки';return}
 try{const d=await api('/api/manager/cell-audit/locate?code='+encodeURIComponent(code),{cache:'no-store'});
  drawLocation(d);msg('Ячейка найдена. Теперь сканируй товар.');}
 catch(e){el('locResult').textContent=e.message;msg(e.message,'bad')}
}
async function preview(){
 if(scanBusy)return;
 if(!locationData){msg('Сначала выбери и проверь ячейку','bad');return}
 const barcode=el('barcode').value.trim(),article=el('article').value.trim();
 if(Boolean(barcode)===Boolean(article)){msg('Введи один вариант: ШК товара или точный артикул','bad');return}
 clearPreview();el('preview').disabled=true;
 try{
  const d=await api('/api/manager/cell-audit/preview',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({location:locationData.location.id,barcode,article})
  });
  previewData=d;
  el('details').innerHTML='<div class="info"><b>'+escapeHTML(d.location.label)+'</b>'+
   '<p><b>'+escapeHTML(d.product.article)+'</b> · '+escapeHTML(d.product.name||'')+'</p>'+
   '<span class="pill">'+(d.product.method==='barcode'?'Проверено по ШК':'Проверено по точному артикулу')+'</span>'+
   (d.previous?'<p class="muted">Ранее: '+escapeHTML(d.previous.article)+'</p>':'<p class="muted">Ячейка пока свободна в адресном справочнике</p>')+'</div>';
  const replacing=!!(d.previous&&d.previous.product_id!==d.product.id);
  el('replaceWarning').classList.toggle('hidden',!replacing);
  el('save').textContent=replacing?'Подтвердить ЗАМЕНУ артикула':'Закрепить товар за ячейкой';
  el('save').disabled=false;msg('Проверь название товара и ячейку. Затем подтверди.');
 }catch(e){msg(e.message,'bad')}finally{el('preview').disabled=false}
}
async function save(){
 if(!previewData||saving)return;
 const d=previewData,replacing=!!(d.previous&&d.previous.product_id!==d.product.id);
 if(replacing&&!confirm('ЗАМЕНА товара '+d.previous.article+' на '+d.product.article+' в '+d.location.label+'. Физически проверил?'))return;
 saving=true;el('save').disabled=true;
 try{
  const result=await api('/api/manager/cell-audit/assign',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({location:d.location.id,
     barcode:d.product.method==='barcode'?d.product.source:'',
     article:d.product.method==='article'?d.product.article:'',
     confirmed_product_id:d.product.id,
     expected_current_id:d.previous?d.previous.product_id:null,
     replace:replacing})
  });
  msg(result.unchanged?'Уже закреплено. Без повторной записи.':
       '✓ Сохранено: '+result.location.label+' → '+result.article,'good');
  previewData=null;el('save').disabled=true;
  locationData=null;
  el('locResult').textContent='Привязка сохранена. Для продолжения выбери следующую ячейку.';
 }catch(e){msg(e.message,'bad');el('save').disabled=false}
 finally{saving=false}
}
async function moveAssignment(){
 if(!locationData||!locationData.assigned){msg('Сначала найди занятую ячейку','bad');return}
 const source=locationData.location,target=el('moveTarget').value.trim();
 if(!target){msg('Укажи новую ячейку','bad');return}
 if(!confirm('Перенести '+locationData.assigned.article+' из '+source.label+
             ' в '+target+'? Новая ячейка должна быть пустой.'))return;
 const b=el('moveButton');b.disabled=true;
 try{
  const result=await api('/api/manager/cell-audit/move',{
   method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({source:source.id,target,
     expected_product_id:locationData.assigned.product_id})
  });
  el('location').value=result.target.label;await locate();
  msg('✓ Перенесено: '+result.article+' · '+result.source.label+' → '+result.target.label,'good');
 }catch(e){msg(e.message,'bad')}
 finally{b.disabled=false}
}
async function stopCamera(){
 if(!scanner)return;
 try{await scanner.stop()}catch(_e){}
 try{scanner.clear()}catch(_e){}
 scanner=null;scanBusy=false;el('cameraHelp').textContent='';
}
async function scan(which){
 if(saving)return;
 if(scanner){await stopCamera();return}
 if(typeof Html5Qrcode==='undefined'){msg('Камера недоступна. Используй Bluetooth/HID сканер или ввод','bad');return}
 const formats=(which==='location'?
  [Html5QrcodeSupportedFormats.CODE_128,Html5QrcodeSupportedFormats.QR_CODE]:
  [Html5QrcodeSupportedFormats.EAN_13,Html5QrcodeSupportedFormats.EAN_8,
   Html5QrcodeSupportedFormats.CODE_128,Html5QrcodeSupportedFormats.QR_CODE]);
 try{
  scanner=new Html5Qrcode('reader',{formatsToSupport:formats});
  scanBusy=true;el('cameraHelp').textContent='Камера активна. Повторно нажми кнопку скана, чтобы закрыть.';
  await scanner.start({facingMode:'environment'},{fps:10,qrbox:{width:240,height:180}},
   async(code)=>{
    await stopCamera();
    if(which==='location'){el('location').value=code;await locate()}
    else{el('barcode').value=code;el('article').value='';await preview()}
   },()=>{});
 }catch(e){await stopCamera();msg('Камера не запустилась. Разреши доступ или используй сканер.','bad')}
}
el('checkLocation').addEventListener('click',locate);
el('preview').addEventListener('click',preview);
el('save').addEventListener('click',save);
el('moveButton').addEventListener('click',moveAssignment);
el('scanLocation').addEventListener('click',()=>scan('location'));
el('scanProduct').addEventListener('click',()=>scan('product'));
el('location').addEventListener('input',()=>{locationData=null;clearPreview();el('movePanel').classList.add('hidden')});
el('barcode').addEventListener('input',clearPreview);
el('article').addEventListener('input',clearPreview);
el('location').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();locate()}});
el('barcode').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();preview()}});
el('article').addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();preview()}});
el('next').addEventListener('click',async()=>{
 await stopCamera();el('location').value='';el('barcode').value='';el('article').value='';
 locationData=null;clearPreview();el('movePanel').classList.add('hidden');el('locResult').textContent='Введи адрес или выбери следующую ячейку на карте.';
 msg('Готово к следующей ячейке');el('location').focus();
});
const initial=new URLSearchParams(location.search).get('location_id');
if(initial){el('location').value=initial;locate()}else{el('location').focus()}
</script></body></html>"""
