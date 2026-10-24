"""Isolated partner pallet pre-advice. Does NOT post stock to MoySklad."""
import base64
import hashlib
import hmac
import io
import secrets
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from html import escape

import qrcode
from flask import Blueprint, abort, jsonify, redirect, request, session, url_for, Response
from openpyxl import load_workbook
from wms import db_connect, manager_access, sql, DATABASE_URL

partner_bp = Blueprint("partners", __name__)
MAX_UPLOAD = 2 * 1024 * 1024
MAX_ROWS = 5000

def now():
    return datetime.now(timezone.utc).isoformat()

def db_init():
    statements = [
        """CREATE TABLE IF NOT EXISTS partner_accounts (
            id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS partner_uploads (
            id TEXT PRIMARY KEY, partner_id TEXT NOT NULL, filename TEXT NOT NULL,
            created_at TEXT NOT NULL, pallet_count INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS partner_pallets (
            id TEXT PRIMARY KEY, partner_id TEXT NOT NULL, upload_id TEXT NOT NULL,
            external_ref TEXT NOT NULL, barcode TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created', accepted_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS partner_pallet_lines (
            id TEXT PRIMARY KEY, pallet_id TEXT NOT NULL, sku TEXT NOT NULL,
            quantity INTEGER NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS partner_pallet_changes (id TEXT PRIMARY KEY, pallet_id TEXT NOT NULL, partner_id TEXT NOT NULL, action TEXT NOT NULL, old_sku TEXT, old_quantity INTEGER, new_sku TEXT, new_quantity INTEGER, reason TEXT NOT NULL, changed_at TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS idx_partner_pallet_owner ON partner_pallets(partner_id, created_at)",
    ]
    with db_connect() as db:
        cur = db.cursor()
        for statement in statements:
            cur.execute(statement)
        if DATABASE_URL:
            cur.execute("ALTER TABLE partner_pallets ADD COLUMN IF NOT EXISTS accepted_at TEXT")
        else:
            columns = [row[1] for row in cur.execute("PRAGMA table_info(partner_pallets)").fetchall()]
            if "accepted_at" not in columns:
                cur.execute("ALTER TABLE partner_pallets ADD COLUMN accepted_at TEXT")
        db.commit()

def resolve_receiving_pallet(barcode):
    """Read-only pallet metadata; a code is never looked up as a MoySklad pack."""
    if not str(barcode).startswith("ONIX-PAL-"):
        return None
    db_init()
    with db_connect() as db:
        cur = db.cursor()
        row = cur.execute(sql("""SELECT p.id, p.partner_id, a.name, p.external_ref, p.status, p.accepted_at
            FROM partner_pallets p JOIN partner_accounts a ON a.id=p.partner_id
            WHERE p.barcode=?"""), (barcode,)).fetchone()
        if not row:
            return None
        lines = cur.execute(sql("SELECT sku, quantity FROM partner_pallet_lines WHERE pallet_id=?"),
                            (row[0],)).fetchall()
    return dict(id=row[0], partner_id=row[1], partner=row[2], reference=row[3],
                status=row[4], accepted_at=row[5], lines=[dict(article=r[0], quantity=int(r[1])) for r in lines])


def claim_receiving_pallet(pallet_id):
    """Single winner for concurrent receiving scans. Rollback is explicit."""
    with db_connect() as db:
        cur = db.cursor()
        cur.execute(sql("UPDATE partner_pallets SET status='processing' WHERE id=? AND status='created'"),
                    (pallet_id,))
        updated = cur.rowcount
        db.commit()
    return updated == 1


def set_receiving_pallet_status(pallet_id, status):
    if status not in ("accepted", "uncertain", "created"):
        raise ValueError("Unsupported status")
    with db_connect() as db:
        db.cursor().execute(sql("""UPDATE partner_pallets SET status=?,
            accepted_at=CASE WHEN ?='accepted' THEN COALESCE(accepted_at, ?) ELSE accepted_at END
            WHERE id=? AND status='processing'"""),
            (status, status, now(), pallet_id))
        db.commit()


def password_hash(password):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260000)
    return salt.hex() + ":" + dk.hex()

def password_valid(password, stored):
    try:
        salt, expected = stored.split(":")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 260000)
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (TypeError, ValueError):
        return False

def partner():
    pid = session.get("partner_id")
    if not pid:
        return None
    with db_connect() as db:
        row = db.cursor().execute(sql("SELECT id, name FROM partner_accounts WHERE id=? AND active=1"), (pid,)).fetchone()
    return row

def require_partner():
    p = partner()
    if not p:
        abort(401)
    return p

def csrf():
    if not session.get("partner_csrf"):
        session["partner_csrf"] = secrets.token_urlsafe(24)
    return session["partner_csrf"]

def require_csrf():
    if not hmac.compare_digest(str(request.form.get("csrf", "")), str(csrf())):
        abort(403)

def page(title, body):
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(title)}</title><style>body{{font:16px system-ui;max-width:920px;margin:2rem auto;padding:1rem;background:#f1f5f9;color:#223}}
    section{{background:white;border-radius:14px;padding:20px;margin:15px 0}}input,button{{font:inherit;padding:10px;margin:5px}}
    table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:9px;text-align:left}}
    a{{color:#2563eb}}.error{{color:#b91c1c}}</style><h1>{escape(title)}</h1>{body}</html>"""

@partner_bp.route("/partners/login", methods=["GET", "POST"])
def login():
    db_init()
    error = ""
    if request.method == "POST":
        # No partner sessions are created from manager or employee credentials.
        name = str(request.form.get("name", "")).strip()
        password = str(request.form.get("password", ""))
        with db_connect() as db:
            row = db.cursor().execute(sql("SELECT id, password_hash FROM partner_accounts WHERE name=? AND active=1"), (name,)).fetchone()
        if row and password_valid(password, row[1]):
            session.clear()
            session["partner_id"] = row[0]
            csrf()
            return redirect("/partners")
        error = "Неверный логин или пароль"
    return page("ONIX · Вход партнёра", f"""<section><p class="error">{escape(error)}</p>
      <form method="post"><input name="name" placeholder="Логин" required autocomplete="username">
      <input type="password" name="password" placeholder="Пароль" required autocomplete="current-password">
      <button>Войти</button></form></section>""")

@partner_bp.post("/partners/logout")
def logout():
    require_partner()
    require_csrf()
    session.clear()
    return redirect("/partners/login")

@partner_bp.post("/api/partners/accounts")
def create_account():
    db_init()
    if not manager_access():
        abort(403)
    body = request.get_json(silent=True) or {}
    name = str(body.get("name", "")).strip()
    password = str(body.get("password", ""))
    if not 2 <= len(name) <= 100 or len(password) < 12:
        return jsonify(error="Имя 2–100 символов, пароль от 12 символов"), 400
    try:
        with db_connect() as db:
            db.cursor().execute(sql("INSERT INTO partner_accounts(id,name,password_hash,created_at) VALUES(?,?,?,?)"),
                (str(uuid.uuid4()), name, password_hash(password), now()))
            db.commit()
    except Exception:
        return jsonify(error="Не удалось создать партнёра. Проверь уникальность имени."), 409
    return jsonify(ok=True, name=name), 201

@partner_bp.route("/manager/partners", methods=["GET", "POST"])
def manager_partners():
    db_init()
    if not manager_access():
        abort(403)
    notice = ""
    if request.method == "POST":
        require_csrf()
        name = str(request.form.get("name", "")).strip()
        password = str(request.form.get("password", ""))
        if not 2 <= len(name) <= 100 or len(password) < 12:
            notice = "Имя 2–100 символов, пароль от 12 символов"
        else:
            try:
                with db_connect() as db:
                    db.cursor().execute(sql("INSERT INTO partner_accounts(id,name,password_hash,created_at) VALUES(?,?,?,?)"),
                                        (str(uuid.uuid4()), name, password_hash(password), now()))
                    db.commit()
                notice = "Партнёр создан. Передай ему логин, пароль и адрес /partners/login"
            except Exception:
                notice = "Логин уже занят или произошла ошибка"
    with db_connect() as db:
        partners = db.cursor().execute(sql("SELECT name, created_at, active FROM partner_accounts ORDER BY created_at DESC")).fetchall()
    rows = "".join(f"<tr><td>{escape(name)}</td><td>{escape(created[:16])}</td><td>{'Активен' if active else 'Отключён'}</td></tr>"
                   for name, created, active in partners)
    return page("ONIX · Управление партнёрами", f"""<p><a href="/manager">← Панель руководителя</a></p>
      <section><h2>Создать ограниченный доступ партнёру</h2><p>{escape(notice)}</p>
      <form method="post"><input type="hidden" name="csrf" value="{csrf()}"><input name="name" placeholder="Поставщик / логин" required minlength="2" maxlength="100">
        <input name="password" placeholder="Пароль (от 12 символов)" required minlength="12" autocomplete="new-password">
        <button>Создать партнёра</button></form></section>
      <section><h2>Партнёры</h2><table><tr><th>Логин</th><th>Создан</th><th>Статус</th></tr>{rows}</table></section>""")


def parse_excel(file):
    """Accept simple SKU/qty rows or ONIX purchase orders like the Promsiz file."""
    if not file.filename.lower().endswith(".xlsx"):
        raise ValueError("Загрузи файл .xlsx")
    raw = file.stream.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise ValueError("Размер файла не больше 2 МБ")
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        sheet = wb["Заказ"] if "Заказ" in wb.sheetnames else wb.active
        records = list(sheet.iter_rows(values_only=True))
        header_row = None
        for idx, row in enumerate(records[:40]):
            cells = [str(c or "").strip().casefold() for c in row]
            if "артикул" in cells and ("количество" in cells or "количество, ед." in cells or "палет" in cells):
                header_row = idx
                break
        if header_row is None:
            raise ValueError("Нет таблицы с колонкой Артикул и количеством")
        headings = [str(c or "").strip().casefold() for c in records[header_row]]
        def column(*aliases):
            return next((headings.index(key) for key in aliases if key in headings), None)
        a = column("артикул", "sku")
        pallets = column("палет")
        each = column("ед. в палете")
        quantity = column("количество", "количество, ед.", "qty", "кол-во")
        group_column = column("палета", "паллет", "pallet")
        if quantity is None and (pallets is None or each is None):
            raise ValueError("Нужны количество либо столбцы Палет и Ед. в палете")
        def positive(value, num):
            try:
                n = int(value)
                if n <= 0 or float(value) != n:
                    raise ValueError()
                return n
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"Строка {num}: ожидается положительное целое число")
        groups = OrderedDict()
        for num, row in enumerate(records[header_row + 1:], start=header_row + 2):
            if not any(x is not None and str(x).strip() for x in row):
                continue
            sku = str(row[a] if a < len(row) and row[a] is not None else "").strip()
            if not sku or sku.casefold().startswith(("итого", "всего")):
                continue
            if len(sku) > 150:
                raise ValueError(f"Строка {num}: артикул слишком длинный")
            if pallets is not None and each is not None:
                count = positive(row[pallets] if pallets < len(row) else None, num)
                per_pallet = positive(row[each] if each < len(row) else None, num)
                if quantity is not None and quantity < len(row) and row[quantity] is not None:
                    total = positive(row[quantity], num)
                    if count * per_pallet != total:
                        raise ValueError(f"Строка {num}: количество не сходится с палетами")
                if len(groups) + count > MAX_ROWS:
                    raise ValueError("Не более 5000 палет в одной загрузке")
                for index in range(1, count + 1):
                    groups[f"{sku} · {index}/{count} · строка {num}"] = [(sku, per_pallet)]
            else:
                qty = positive(row[quantity] if quantity < len(row) else None, num)
                ref = str(row[group_column]).strip() if group_column is not None and group_column < len(row) and row[group_column] is not None else ""
                key = ref if group_column is not None else f"Строка {num}"
                if not key:
                    raise ValueError(f"Строка {num}: не указан номер палеты")
                groups.setdefault(key, []).append((sku, qty))
                if len(groups) > MAX_ROWS:
                    raise ValueError("Не более 5000 палет в одной загрузке")
        if not groups:
            raise ValueError("Нет строк с товарами")
        return groups
    finally:
        wb.close()


@partner_bp.route("/partners", methods=["GET"])
def dashboard():
    db_init()
    p = require_partner()
    with db_connect() as db:
        rows = db.cursor().execute(sql("""SELECT id, barcode, external_ref, created_at, status FROM partner_pallets
            WHERE partner_id=? ORDER BY created_at DESC LIMIT 200"""), (p[0],)).fetchall()
    items = "".join(f"<tr><td>{escape(str(r[2]))}</td><td>{escape(r[1])}</td><td>{escape(r[4])}</td><td>{escape(r[3][:16])}</td><td><a href='/partners/pallet/{r[0]}/print'>Печать</a>"+(f" · <a href='/partners/pallet/{r[0]}/edit'>Изменить</a>" if r[4]=='created' else '')+"</td></tr>" for r in rows)
    return page("ONIX · Палеты партнёра", f"""<p>Партнёр: {escape(p[1])}</p>
    <section><h2>Добавить палету вручную</h2><form method="post" action="/partners/pallet/new"><input type="hidden" name="csrf" value="{csrf()}"><input name="article" placeholder="Артикул" required><input name="quantity" type="number" min="1" step="1" required><input name="reason" placeholder="Причина" required><button>Создать палету</button></form>{partner_catalog_widget()}</section><section><h2>Загрузить Excel</h2><p>Поддерживается файл заказа с колонками <b>Артикул</b>, <b>Палет</b>, <b>Ед. в палете</b> и простой файл с <b>Артикул</b>, <b>Количество</b>, <b>Палета</b> (необязательно).
    Одинаковая «Палета» объединяет строки в одну палету. Без этого столбца каждая строка создаёт отдельную палету.</p>
    <form method="post" action="/partners/upload" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{csrf()}"><input type="file" name="file" accept=".xlsx" required>
    <button>Создать палеты и ШК</button></form></section><section><h2>История: последние 200 палет</h2>
    <table><tr><th>Палета</th><th>ШК</th><th>Статус</th><th>Дата UTC</th><th></th></tr>{items}</table></section>
    <form method="post" action="/partners/logout"><input type="hidden" name="csrf" value="{csrf()}"><button>Выйти</button></form>""")



@partner_bp.get("/partners/catalog/search")
def partner_catalog_search():
    require_partner()
    q=str(request.args.get("q","")).strip()
    if len(q)<2:
        return jsonify(items=[])
    if len(q)>100:
        return jsonify(error="Слишком длинный запрос"),400
    # Read-only snapshot, already refreshed from MoySklad by ONIX.
    with db_connect() as db:
        rows=db.cursor().execute(sql("""SELECT article, MAX(name),
                COUNT(DISTINCT CASE WHEN is_pack=1 AND quantity>1 AND quantity=CAST(quantity AS INTEGER) THEN CAST(quantity AS INTEGER) END),
                MAX(CASE WHEN is_pack=1 AND quantity>1 AND quantity=CAST(quantity AS INTEGER) THEN CAST(quantity AS INTEGER) END)
            FROM fbs_catalog_barcodes
            WHERE article<>'' AND (LOWER(article) LIKE ? OR LOWER(name) LIKE ?)
            GROUP BY article ORDER BY article LIMIT 20"""),
            ("%"+q.lower()+"%","%"+q.lower()+"%")).fetchall()
    return jsonify(items=[{"article":row[0],"name":row[1], "pack_qty":int(row[3]) if row[2]==1 and row[3] else None} for row in rows])


def partner_catalog_widget():
    return """<datalist id="partner-sku-results"></datalist>
      <p id="partner-sku-name" style="font-size:14px;color:#52647a"></p>
      <script>
      (()=>{
        const field=document.querySelector('input[name="article"]');
        const results=document.getElementById('partner-sku-results');
        const info=document.getElementById('partner-sku-name');
        const quantity=document.querySelector('input[name="quantity"]');
        let lastSelected='';
        if(!field||!results)return;
        field.setAttribute('list','partner-sku-results');
        let seq=0;
        async function search(){
          const q=field.value.trim(),current=++seq;
          if(q.length<2){results.replaceChildren();info.textContent='Введите минимум 2 символа артикула или названия';lastSelected='';return}
          try{
            const response=await fetch('/partners/catalog/search?q='+encodeURIComponent(q),{cache:'no-store'});
            if(!response.ok)throw Error();
            const data=await response.json();
            if(current!==seq)return;
            results.replaceChildren();
            for(const entry of data.items){
              const option=document.createElement('option');
              option.value=entry.article;
              option.label=entry.name;
              results.appendChild(option);
            }
            const exact=data.items.find(x=>x.article===field.value.trim());
            info.textContent=exact?(exact.name+(exact.pack_qty?' · квант '+exact.pack_qty+' шт.':' · квант не определён')):(data.items.length?'Выбери артикул из списка':'Товар в каталоге не найден');
            if(exact && exact.article!==lastSelected){
              lastSelected=exact.article;
              if(quantity && exact.pack_qty)quantity.value=String(exact.pack_qty);
            }else if(!exact){lastSelected='';}
          }catch(e){if(current===seq)info.textContent='Каталог временно недоступен'}
        }
        field.addEventListener('input',search);
        field.addEventListener('change',search);
        if(field.value)search();
      })();
      </script>"""


def _manual_fields(form):
    sku=str(form.get("article","")).strip()
    raw=str(form.get("quantity","")).strip()
    reason=str(form.get("reason","")).strip()
    if not sku or len(sku)>150 or not raw.isascii() or not raw.isdecimal() or not 1<=int(raw)<=1000000:
        raise ValueError("Укажи артикул и положительное целое количество")
    if not reason or len(reason)>300:
        raise ValueError("Укажи причину изменения")
    with db_connect() as db:
        found=db.cursor().execute(sql("SELECT 1 FROM fbs_catalog_barcodes WHERE article=? LIMIT 1"),(sku,)).fetchone()
    if not found:
        raise ValueError("Артикул не найден в каталоге ONIX. Выбери товар из подсказок.")
    return sku,int(raw),reason


@partner_bp.post("/partners/pallet/new")
def manual_create():
    db_init()
    p=require_partner()
    require_csrf()
    try:
        sku,qty,reason=_manual_fields(request.form)
    except ValueError as exc:
        return page("Ошибка",escape(str(exc))),400
    pid,uid=str(uuid.uuid4()),str(uuid.uuid4())
    with db_connect() as db:
        cur=db.cursor()
        cur.execute(sql("INSERT INTO partner_uploads(id,partner_id,filename,created_at,pallet_count) VALUES (?,?,?,?,?)"),(uid,p[0],"вручную",now(),1))
        cur.execute(sql("INSERT INTO partner_pallets(id,partner_id,upload_id,external_ref,barcode,created_at) VALUES (?,?,?,?,?,?)"),(pid,p[0],uid,"Добавлена вручную","ONIX-PAL-"+uuid.uuid4().hex.upper(),now()))
        cur.execute(sql("INSERT INTO partner_pallet_lines(id,pallet_id,sku,quantity) VALUES (?,?,?,?)"),(str(uuid.uuid4()),pid,sku,qty))
        cur.execute(sql("INSERT INTO partner_pallet_changes(id,pallet_id,partner_id,action,new_sku,new_quantity,reason,changed_at) VALUES (?,?,?,?,?,?,?,?)"),(str(uuid.uuid4()),pid,p[0],"create",sku,qty,reason,now()))
        db.commit()
    return redirect("/partners")


@partner_bp.route("/partners/pallet/<pallet_id>/edit",methods=["GET","POST"])
def manual_edit(pallet_id):
    db_init()
    p=require_partner()
    if request.method=="POST":
        require_csrf()
    with db_connect() as db:
        cur=db.cursor()
        row=cur.execute(sql("SELECT id,status FROM partner_pallets WHERE id=? AND partner_id=?"),(pallet_id,p[0])).fetchone()
        if not row:
            abort(404)
        if row[1]!="created":
            return page("Изменение запрещено","Палета в обработке или уже принята"),409
        lines=cur.execute(sql("SELECT id,sku,quantity FROM partner_pallet_lines WHERE pallet_id=?"),(pallet_id,)).fetchall()
        if len(lines)!=1:
            return page("Изменение запрещено","Редактирование смешанной палеты пока недоступно"),409
        if request.method=="POST":
            try:
                sku,qty,reason=_manual_fields(request.form)
            except ValueError as exc:
                return page("Ошибка",escape(str(exc))),400
            cur.execute(sql("UPDATE partner_pallets SET external_ref=external_ref WHERE id=? AND partner_id=? AND status='created'"),(pallet_id,p[0]))
            if cur.rowcount!=1:
                return page("Изменение запрещено","Палета уже в обработке"),409
            cur.execute(sql("UPDATE partner_pallet_lines SET sku=?,quantity=? WHERE id=?"),(sku,qty,lines[0][0]))
            cur.execute(sql("INSERT INTO partner_pallet_changes(id,pallet_id,partner_id,action,old_sku,old_quantity,new_sku,new_quantity,reason,changed_at) VALUES (?,?,?,?,?,?,?,?,?,?)"),(str(uuid.uuid4()),pallet_id,p[0],"edit",lines[0][1],lines[0][2],sku,qty,reason,now()))
            db.commit()
            return redirect("/partners")
    with db_connect() as db:
        entries=db.cursor().execute(sql("SELECT old_sku,old_quantity,new_sku,new_quantity,reason,changed_at FROM partner_pallet_changes WHERE pallet_id=? AND partner_id=? ORDER BY changed_at DESC"),(pallet_id,p[0])).fetchall()
    history="".join("<li>"+escape(str(e))+"</li>" for e in entries)
    return page("Изменить палету",'<p><a href="/partners">Назад</a></p><section><form method="post"><input type="hidden" name="csrf" value="'+csrf()+'"><input name="article" value="'+escape(lines[0][1],quote=True)+'" required><input type="number" name="quantity" min="1" step="1" value="'+str(lines[0][2])+'" required><input name="reason" placeholder="Причина изменения" required><button>Сохранить</button></form><p>QR останется прежним</p></section><section><h2>История изменений</h2><ul>'+history+'</ul></section>'+partner_catalog_widget())


@partner_bp.post("/partners/upload")
def upload():
    db_init()
    p = require_partner()
    require_csrf()
    file = request.files.get("file")
    if not file:
        abort(400)
    try:
        groups = parse_excel(file)
    except (ValueError, OSError, StopIteration) as exc:
        return page("Ошибка Excel", f'<section class="error">{escape(str(exc))}</section><a href="/partners">Назад</a>'), 400
    upload_id = str(uuid.uuid4())
    with db_connect() as db:
        cur = db.cursor()
        cur.execute(sql("INSERT INTO partner_uploads(id,partner_id,filename,created_at,pallet_count) VALUES(?,?,?,?,?)"),
                    (upload_id, p[0], file.filename[:255], now(), len(groups)))
        for ref, lines in groups.items():
            pallet_id = str(uuid.uuid4())
            barcode = "ONIX-PAL-" + uuid.uuid4().hex.upper()
            cur.execute(sql("""INSERT INTO partner_pallets(id,partner_id,upload_id,external_ref,barcode,created_at)
                VALUES(?,?,?,?,?,?)"""), (pallet_id, p[0], upload_id, ref, barcode, now()))
            for sku, qty in lines:
                cur.execute(sql("INSERT INTO partner_pallet_lines(id,pallet_id,sku,quantity) VALUES(?,?,?,?)"),
                            (str(uuid.uuid4()), pallet_id, sku, qty))
        db.commit()
    return redirect("/partners")

@partner_bp.get("/partners/pallet/<pallet_id>/print")
def print_label(pallet_id):
    db_init()
    p = require_partner()
    with db_connect() as db:
        row = db.cursor().execute(sql("""SELECT barcode,external_ref FROM partner_pallets
            WHERE id=? AND partner_id=?"""), (pallet_id, p[0])).fetchone()
    if not row:
        abort(404)
    qr = qrcode.make(row[0])
    out = io.BytesIO()
    qr.save(out, format="PNG")
    encoded = base64.b64encode(out.getvalue()).decode()
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><title>ONIX · Палета {escape(row[1])}</title>
    <style>
    @page{{size:A4 portrait;margin:0}}
    *{{box-sizing:border-box}}
    body{{margin:0;font-family:Arial,sans-serif;color:#111}}
    .sheet{{width:210mm;height:297mm;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:15mm;text-align:center;break-after:page}}
    .heading{{font-size:32pt;font-weight:800;margin:0 0 15mm}}
    .ref{{font-size:25pt;font-weight:700;overflow-wrap:anywhere;max-width:175mm;margin-bottom:10mm}}
    img{{width:145mm;height:145mm;image-rendering:pixelated}}
    .code{{font-size:13pt;font-weight:700;overflow-wrap:anywhere;max-width:180mm;margin-top:10mm}}
    button{{font:18px Arial;padding:12px 20px;margin:16px}}
    @media print{{button{{display:none}}}}
    </style>
    <button onclick="window.print()">Печать A4</button>
    <div class="sheet"><h1 class="heading">ONIX · ПАЛЕТА</h1><div class="ref">{escape(row[1])}</div>
    <img alt="QR палеты" src="data:image/png;base64,{encoded}">
    <div class="code">{escape(row[0])}</div></div></html>"""
