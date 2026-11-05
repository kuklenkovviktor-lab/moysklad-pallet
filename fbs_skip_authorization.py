"""GRIT FBS supervisor-issued, one-use QR authorization for skipping an item.

The QR does NOT confirm an order or change stock. It can only release an
existing server-side pending product in an active employee picking shift.
"""
import hashlib
import io
import base64
import re
import secrets
import time

TOKEN_TTL_SECONDS = 90
TOKEN_RE = re.compile(r"97[0-9]{22}\Z")


def issue_skip_code(issued_by="Руководитель (ПК)"):
    from wms import ensure_db, execute, now_iso
    ensure_db()
    code = "97" + f"{secrets.randbelow(10**22):022d}"
    digest = hashlib.sha256(code.encode("ascii")).hexdigest()
    expiry = time.time() + TOKEN_TTL_SECONDS
    execute(
        "INSERT INTO fbs_skip_tokens "
        "(token_hash, issued_by, issued_at, expires_at) VALUES (?, ?, ?, ?)",
        (digest, issued_by, now_iso(), expiry),
    )
    return code, expiry


def qr_png_data_url(code):
    import qrcode
    q = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=3)
    q.add_data(code)
    q.make(fit=True)
    buffer = io.BytesIO()
    q.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def pending_for_shift(shift_id, employee_id):
    from wms import query_one
    row = query_one(
        "SELECT product_barcode, article, product_name FROM fbs_pending_products "
        "WHERE shift_id=? AND employee_id=?",
        (shift_id, employee_id),
    )
    return {"barcode": row[0], "article": row[1] or "", "name": row[2] or ""} if row else None


def record_pending_item(shift_id, employee_id, barcode, article, name):
    from wms import query_one, execute, now_iso, current_activity
    if not shift_id or not employee_id:
        raise ValueError("Требуется активная смена для сборки")
    active = query_one(
        "SELECT 1 FROM shifts WHERE id=? AND employee_id=? AND ended_at IS NULL",
        (shift_id, employee_id),
    )
    op = current_activity(shift_id)
    if not active or not op or op.get("type") != "picking":
        raise ValueError("Сначала выбери операцию «Сборка»")
    existing = pending_for_shift(shift_id, employee_id)
    if existing and existing["barcode"] != barcode:
        raise ValueError("Сначала заверши текущий товар либо отсканируй QR руководителя «Пропуск товара»")
    if existing:
        return existing
    execute(
        "INSERT INTO fbs_pending_products "
        "(shift_id, employee_id, product_barcode, article, product_name, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (shift_id) DO NOTHING",
        (shift_id, employee_id, barcode, article or "", name or "", now_iso()),
    )
    result = pending_for_shift(shift_id, employee_id)
    if not result or result["barcode"] != barcode:
        raise ValueError("Другая позиция уже ожидает стикер WB")
    return result


def complete_pending_item(shift_id, employee_id, barcode):
    from wms import execute
    execute(
        "DELETE FROM fbs_pending_products WHERE shift_id=? AND employee_id=? AND product_barcode=?",
        (shift_id, employee_id, barcode),
    )


def consume_skip_code(code, shift_id, employee_id):
    """Atomic, single-use; fails closed if token expired, used or no pending item."""
    from wms import db_connect, sql, now_iso
    candidate = str(code or "").strip()
    if not TOKEN_RE.fullmatch(candidate):
        raise ValueError("Это не QR пропуска товара")
    digest = hashlib.sha256(candidate.encode("ascii")).hexdigest()
    ts = now_iso()
    with db_connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql(
                "SELECT product_barcode, article, product_name FROM fbs_pending_products "
                "WHERE shift_id=? AND employee_id=?"
            ), (shift_id, employee_id))
            pending = cur.fetchone()
            if not pending:
                raise ValueError("Нет товара, ожидающего пропуска")
            cur.execute(sql(
                "UPDATE fbs_skip_tokens "
                "SET consumed_at=?, consumed_shift_id=?, consumed_employee_id=?, consumed_product_barcode=? "
                "WHERE token_hash=? AND consumed_at IS NULL AND expires_at>?"
            ), (ts, shift_id, employee_id, pending[0], digest, time.time()))
            if cur.rowcount != 1:
                raise ValueError("QR пропуска уже использован или срок действия истёк")
            cur.execute(sql(
                "DELETE FROM fbs_pending_products WHERE shift_id=? AND employee_id=? AND product_barcode=?"
            ), (shift_id, employee_id, pending[0]))
            if cur.rowcount != 1:
                raise ValueError("Товар уже изменился; QR не применён")
            conn.commit()
            return {"barcode": pending[0], "article": pending[1] or "", "name": pending[2] or ""}
        except Exception:
            conn.rollback()
            raise
        finally:
            cur.close()


MANAGER_SKIP_PAGE = """<!doctype html><html lang="ru"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Cache-Control" content="no-store">
<title>GRIT WMS · Пропуск товара</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;background:#09121e;color:#eff7ff;font-family:system-ui,Segoe UI,sans-serif;padding:30px 18px}
.wrap{max-width:660px;margin:auto}.nav{display:inline-block;margin-bottom:18px;color:#75c2ff;text-decoration:none}
.card{background:#14283a;border:1px solid #36536b;border-radius:14px;padding:24px}
h1{font-size:27px;margin:0 0 10px}.lead{color:#adc5d8;font-size:14px;line-height:1.55}
#qrArea{display:none;margin:22px auto;text-align:center}
#qrImage{display:block;width:min(340px,90%);height:auto;margin:12px auto;background:white;padding:12px;border-radius:12px}
button{background:#e58a34;border:0;border-radius:9px;color:#101a24;font-size:16px;font-weight:750;padding:14px 22px;cursor:pointer}
.timer{font-size:30px;font-weight:850;margin:12px} .status{color:#ffbc9a;font-weight:650}
.note{font-size:13px;color:#bad0e0;line-height:1.55;border-top:1px solid #36536b;padding-top:16px;margin-top:20px}
</style></head><body><div class="wrap">
<a class="nav" href="/manager">← Панель руководителя</a>
<div class="card">
<h1>Пропуск товара · QR руководителя</h1>
<p class="lead">Когда товар уже отсканирован, но требуется перейти к следующему,
создай временный QR. Сотрудник сканирует его вместо стикера WB. Пропуск не
засчитывает заказ и фиксируется в журнале.</p>
<button id="create" onclick="createCode()">Создать одноразовый QR</button>
<div id="qrArea">
<img id="qrImage" alt="Временный QR для разрешения пропуска товара">
<div class="timer" id="ttl">90 сек.</div>
<div class="status" id="status">QR действует только для одного пропуска.</div>
</div>
<div class="note">Действует 90 секунд и только один раз. QR генерируется
в менеджерской сессии. После сканирования просканированный товар сбрасывается,
заказ не подтверждается, выработка не увеличивается.
Нажми «Создать» для новой операции.</div>
</div></div>
<script>
let expiry=0,ticker=null;
async function createCode(){
 const button=document.getElementById('create');button.disabled=true;
 try{
  const response=await fetch('/api/manager/fbs/skip-qr',{method:'POST',
   cache:'no-store',headers:{'Content-Type':'application/json'},body:'{}'});
  const data=await response.json();
  if(!response.ok)throw new Error(data.error||'Ошибка генерации QR');
  document.getElementById('qrImage').src=data.image;
  document.getElementById('qrArea').style.display='block';
  expiry=data.expires_at*1000;
  clearInterval(ticker);
  const update=()=>{
   const remaining=Math.max(0,Math.ceil((expiry-Date.now())/1000));
   document.getElementById('ttl').textContent=remaining+' сек.';
   document.getElementById('status').textContent=remaining?'Одноразовый QR, не для постоянной печати.':'QR больше не действует. Создай новый.';
   if(!remaining)clearInterval(ticker);
  };
  ticker=setInterval(update,250);update();
 }catch(e){alert(e.message)}
 finally{button.disabled=false}
}
</script></body></html>"""
