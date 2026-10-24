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
from wms import db_connect, manager_access, sql

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
            created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'created'
        )""",
        """CREATE TABLE IF NOT EXISTS partner_pallet_lines (
            id TEXT PRIMARY KEY, pallet_id TEXT NOT NULL, sku TEXT NOT NULL,
            quantity INTEGER NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_partner_pallet_owner ON partner_pallets(partner_id, created_at)",
    ]
    with db_connect() as db:
        cur = db.cursor()
        for statement in statements:
            cur.execute(statement)
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

def parse_excel(file):
    if not file.filename.lower().endswith(".xlsx"):
        raise ValueError("Загрузи файл .xlsx")
    raw = file.stream.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise ValueError("Размер файла не больше 2 МБ")
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    try:
        sheet = wb.active
        records = sheet.iter_rows(values_only=True)
        headers = next(records, None)
        if not headers:
            raise ValueError("Excel пуст")
        normalized = [str(c or "").strip().casefold() for c in headers]
        synonyms = {"sku": ("артикул", "sku"), "qty": ("количество", "qty", "кол-во"),
                    "pallet": ("палета", "паллет", "pallet")}
        def find(key):
            return next((normalized.index(k) for k in synonyms[key] if k in normalized), None)
        a, q, p = find("sku"), find("qty"), find("pallet")
        if a is None or q is None:
            raise ValueError("Нужны столбцы: Артикул, Количество. Необязательно: Палета")
        groups = OrderedDict()
        count = 0
        for row_num, row in enumerate(records, start=2):
            if not any(x is not None and str(x).strip() for x in row):
                continue
            count += 1
            if count > MAX_ROWS:
                raise ValueError("Не более 5000 строк в файле")
            sku = str(row[a] if a < len(row) and row[a] is not None else "").strip()
            qty_value = row[q] if q < len(row) else None
            try:
                qty = int(qty_value)
                if qty <= 0 or float(qty_value) != qty:
                    raise ValueError()
            except (TypeError, ValueError, OverflowError):
                raise ValueError(f"Строка {row_num}: некорректное количество")
            if not sku or len(sku) > 150:
                raise ValueError(f"Строка {row_num}: некорректный артикул")
            pallet_ref = str(row[p]).strip() if p is not None and p < len(row) and row[p] is not None else ""
            # No pallet column means one independent pallet per row.
            key = pallet_ref if p is not None else f"row-{row_num}"
            if not key or len(key) > 150:
                raise ValueError(f"Строка {row_num}: не указана палета")
            groups.setdefault(key, []).append((sku, qty))
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
    items = "".join(f"<tr><td>{escape(str(r[2]))}</td><td>{escape(r[1])}</td><td>{escape(r[4])}</td><td>{escape(r[3][:16])}</td><td><a href='/partners/pallet/{r[0]}/print'>Печать</a></td></tr>" for r in rows)
    return page("ONIX · Палеты партнёра", f"""<p>Партнёр: {escape(p[1])}</p>
    <section><h2>Загрузить Excel</h2><p>Столбцы: <b>Артикул</b>, <b>Количество</b>, <b>Палета</b> (необязательно).
    Одинаковая «Палета» объединяет строки в одну палету. Без этого столбца каждая строка создаёт отдельную палету.</p>
    <form method="post" action="/partners/upload" enctype="multipart/form-data">
    <input type="hidden" name="csrf" value="{csrf()}"><input type="file" name="file" accept=".xlsx" required>
    <button>Создать палеты и ШК</button></form></section><section><h2>История: последние 200 палет</h2>
    <table><tr><th>Палета</th><th>ШК</th><th>Статус</th><th>Дата UTC</th><th></th></tr>{items}</table></section>
    <form method="post" action="/partners/logout"><input type="hidden" name="csrf" value="{csrf()}"><button>Выйти</button></form>""")

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
    return f"""<!doctype html><html lang="ru"><meta charset="utf-8"><style>
    @page{{size:58mm 40mm;margin:0}}body{{margin:0;font:9px Arial}}
    .label{{width:58mm;height:40mm;display:flex;align-items:center;justify-content:center;gap:2mm}}
    img{{width:35mm;height:35mm}}small{{overflow-wrap:anywhere;max-width:19mm}}
    @media print{{button{{display:none}}}}</style>
    <button onclick="window.print()">Печать 58×40</button><div class="label">
    <img alt="QR палеты" src="data:image/png;base64,{encoded}">
    <small>{escape(row[1])}<hr>{escape(row[0])}</small></div></html>"""
