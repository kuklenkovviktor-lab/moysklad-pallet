"""Dedicated 120 mm x 75 mm landscape labels for WAREHOUSE MAP cells only.

Existing 58 x 40 replenishment QR labels, badges and FBS remain unchanged.
Prints the permanent ONIX-LOC identifier in BOTH QR and Code128 formats.
"""
import base64
import html
import io
import re

import barcode
import qrcode
from barcode.writer import SVGWriter
from flask import Blueprint, Response, redirect, request

from wms import ensure_db, manager_access, query_all

map_labels_bp = Blueprint("warehouse_map_labels", __name__)

# K-01-2 and analogous numbered upper tiers point UP; K-01 points DOWN.
_UPPER_TIER = re.compile(r"^[А-ЯЁA-Z]+-?\d+-[2-9]\d*$", re.IGNORECASE)


def direction_for_location(address):
    return "up" if _UPPER_TIER.fullmatch(str(address).strip()) else "down"


def _locations(location_id=None):
    from cell_audit import ensure_cell_audit_db
    ensure_cell_audit_db()
    where = " AND l.id = ?" if location_id else ""
    params = (location_id,) if location_id else ()
    return query_all("""
        SELECT l.id, l.label, l.barcode, a.article, a.product_name
        FROM replenishment_locations l
        LEFT JOIN cell_sku_assignments a ON a.location_id = l.id
        WHERE l.active = 1 """ + where + """
        ORDER BY l.label, l.id LIMIT 1001
    """, params)


def _qr(payload):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=8, border=4)
    qr.add_data(payload)
    qr.make(fit=True)
    stream = io.BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _code128(payload):
    writer = SVGWriter()
    code = barcode.get("code128", payload, writer=writer)
    stream = io.BytesIO()
    code.write(stream, options={
        "module_width": 0.22, "module_height": 11,
        "quiet_zone": 2.5, "write_text": False,
    })
    return base64.b64encode(stream.getvalue()).decode("ascii")


def _render_label(row):
    _, label, payload, article, product_name = row
    label = html.escape(str(label))
    payload = str(payload).strip()
    direction = direction_for_location(label)
    arrow = "↑" if direction == "up" else "↓"
    # The SKU description can change independently from a permanent cell ID;
    # barcodes MUST never reference the SKU or a previous item code.
    item = str(product_name or article or "ЯЧЕЙКА БЕЗ ЗАКРЕПЛЁННОГО ТОВАРА").strip()
    if len(item) > 110:
        item = item[:107] + "…"
    return (
        '<article class="sheet" data-arrow="' + direction + '">'
        '<div class="product">' + html.escape(item) + '</div>'
        '<div class="main">'
        '<div class="qrcode"><img alt="QR адреса" src="data:image/png;base64,' + _qr(payload) + '"></div>'
        '<div class="address">' + label + '</div>'
        '<div class="arrow" aria-label="' + ("Вверх" if direction == "up" else "Вниз") + '">' + arrow + '</div>'
        '</div>'
        '<div class="bottom"><img alt="Code128 адреса" src="data:image/svg+xml;base64,' + _code128(payload) + '">'
        '<div class="code-text">' + html.escape(payload) + '</div></div>'
        '</article>'
    )


@map_labels_bp.get("/manager/warehouse-map/labels")
def map_location_labels():
    ensure_db()
    if not manager_access():
        return redirect("/manager-login")
    location_id = str(request.args.get("id") or "").strip()
    if len(location_id) > 100:
        return Response("Некорректный идентификатор", status=400)
    rows = _locations(location_id or None)
    if not rows:
        return Response("Активные ячейки не найдены", status=404)
    if len(rows) > 1000:
        return Response("Слишком много ячеек для одного задания печати", status=413)
    sections = "".join(_render_label(row) for row in rows)
    page = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GRIT · Карта склада · этикетки 75×120</title>
<style>
@page { size:120mm 75mm; margin:0 }
*{box-sizing:border-box}
html,body{margin:0;padding:0;font-family:Arial,Helvetica,sans-serif}
body{background:#e8edf2;color:#000}
.tools{margin:12px auto;padding:14px;background:#fff;max-width:650px;text-align:center}
.tools button{font:inherit;padding:11px 20px;cursor:pointer}
.sheet{width:120mm;height:75mm;margin:12px auto;background:#fff;border:1.2mm solid #000;
 display:flex;flex-direction:column;overflow:hidden;break-inside:avoid;page-break-inside:avoid;
 break-after:page;page-break-after:always}
.sheet:last-child{break-after:auto;page-break-after:auto}
.product{height:16mm;display:flex;align-items:center;justify-content:center;text-align:center;
 font-weight:800;font-size:15pt;line-height:1.1;padding:1.6mm 3mm;border-bottom:.65mm solid #000}
.main{height:39mm;display:grid;grid-template-columns:31mm 1fr 22mm;border-bottom:.65mm solid #000}
.qrcode{display:flex;align-items:center;justify-content:center;border-right:.55mm solid #000}
.qrcode img{width:29mm;height:29mm;image-rendering:pixelated}
.address{min-width:0;display:flex;align-items:center;justify-content:center;font-weight:900;
 font-size:36pt;white-space:nowrap;letter-spacing:-.06em;padding:0 1mm;overflow:hidden}
.arrow{border-left:.55mm solid #000;display:flex;justify-content:center;align-items:center;
 font-family:Arial,sans-serif;font-weight:900;font-size:65pt;line-height:1}
.bottom{height:17mm;display:flex;flex-direction:column;align-items:center;justify-content:center;
 overflow:hidden;padding:.3mm 4mm .6mm}
.bottom img{width:104mm;height:11mm;object-fit:fill}
.code-text{font-size:8pt;line-height:3.5mm;letter-spacing:.3mm;font-weight:bold}
@media print{
 html,body{background:#fff;margin:0;padding:0}
 .tools{display:none!important}
 .sheet{margin:0;border:1mm solid #000;width:120mm;height:75mm}
}
</style></head><body>
<div class="tools"><button type="button" onclick="window.print()">Печатать · 75×120 мм</button>
<p>Только ячейки карты склада. Выбери в драйвере горизонтальную этикетку 120×75 мм,
 масштаб 100%, поля 0, без колонтитулов. Этикеток: """ + str(len(rows)) + """</p>
<p>QR и Code128 кодируют постоянный ШК ячейки. При изменении товара заголовок нужно перепечатать.</p></div>
""" + sections + "</body></html>"
    return Response(page, mimetype="text/html",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
