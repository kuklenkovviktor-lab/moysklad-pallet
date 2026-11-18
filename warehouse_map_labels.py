"""Dedicated 120 mm x 75 mm landscape labels for WAREHOUSE MAP cells only.

Existing 58 x 40 replenishment QR labels, badges and FBS remain unchanged.
Keeps permanent ONIX-LOC in QR. Code128 encodes verified PRODUCT barcode only.
"""
import base64
import html
import io
import re

import barcode
import qrcode
from barcode.writer import SVGWriter
from flask import Blueprint, Response, redirect, request

from wms import ensure_db, manager_access, query_all, query_one

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
        SELECT l.id, l.label, l.barcode, a.article, a.product_name, a.product_id, a.source_barcode
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


def _verified_product_barcode(product_id, source_barcode):
    """Use a verified unit product code, never manufacture it from the article.

    A recorded scan can be a packaging code. Prefer a catalog-confirmed unit
    barcode; when multiple unit codes exist, prefer the scanned unit code.
    No automatic substitution of packaging, cell QR or a made-up value.
    """
    if not product_id:
        return ""
    try:
        rows = query_all("""
            SELECT barcode FROM fbs_catalog_barcodes
            WHERE product_id = ? AND is_pack = 0 AND quantity = 1
            ORDER BY barcode LIMIT 100
        """, (product_id,))
    except Exception:
        rows = []
    codes = [str(r[0]).strip() for r in rows if str(r[0]).strip()]
    scanned = str(source_barcode or "").strip()
    if scanned and scanned in codes:
        return scanned
    if len(codes) == 1:
        return codes[0]
    # FBS cache missing or multiple codes without positive identification:
    # printing an arbitrary code is unsafe.
    return ""


def _arrow(direction):
    # Filled vector arrow (not a font glyph): extra-wide head and tall shaft.
    if direction == "up":
        path = "M 50 5 L 6 50 Q 3 54 8 54 L 29 54 L 29 96 L 71 96 L 71 54 L 92 54 Q 97 54 94 50 Z"
    else:
        path = "M 29 4 L 71 4 L 71 46 L 92 46 Q 97 46 94 50 L 50 95 L 6 50 Q 3 46 8 46 L 29 46 Z"
    return ('<svg class="direction-svg" viewBox="0 0 100 100" '
            'xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
            '<path fill="#000" d="' + path + '"/></svg>')


def _render_label(row):
    _, location, location_qr, article, product_name, product_id, source_barcode = row
    label = html.escape(str(location))
    location_qr = str(location_qr).strip()
    direction = direction_for_location(location)
    item = str(product_name or article or "ЯЧЕЙКА БЕЗ ЗАКРЕПЛЁННОГО ТОВАРА").strip()
    if len(item) > 110:
        item = item[:107] + "…"
    sku = str(article or "").strip()
    product_code = _verified_product_barcode(product_id, source_barcode) if sku else ""
    if product_code and len(product_code) > 120:
        product_code = ""
    # Keep QR payload IDENTICAL to the original replenishment identifier.
    barcode_markup = (
        '<img alt="Code128 товара" src="data:image/svg+xml;base64,' +
        _code128(product_code) + '">'
        if product_code else
        '<div class="missing-code">ШТРИХКОД ТОВАРА НЕ ПОДТВЕРЖДЁН</div>'
    )
    return (
        '<article class="sheet" data-arrow="' + direction + '">'
        '<div class="product">' + html.escape(item) + '</div>'
        '<div class="main">'
        '<div class="qrcode"><img alt="QR адреса" src="data:image/png;base64,' + _qr(location_qr) + '"></div>'
        '<div class="address">' + label + '</div>'
        '<div class="arrow" aria-label="' + ("Вверх" if direction == "up" else "Вниз") + '">' +
        _arrow(direction) + '</div></div>'
        '<div class="bottom">' + barcode_markup +
        '<div class="code-text">' + html.escape(sku or "ТОВАР НЕ ЗАКРЕПЛЁН") + '</div></div>'
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
.main{height:39mm;display:grid;grid-template-columns:30mm minmax(0,1fr) 31mm;border-bottom:.65mm solid #000}
.qrcode{display:flex;align-items:center;justify-content:center;border-right:.55mm solid #000}
.qrcode img{width:29mm;height:29mm;image-rendering:pixelated}
.address{min-width:0;display:flex;align-items:center;justify-content:center;font-weight:900;
 font-size:49pt;white-space:nowrap;letter-spacing:-.055em;padding:0 1mm;overflow:hidden}
.arrow{border-left:.55mm solid #000;display:flex;justify-content:center;align-items:center;padding:1mm}
.direction-svg{display:block;width:100%;height:100%;max-width:30mm;max-height:37mm}
.bottom{height:17mm;display:flex;flex-direction:column;align-items:center;justify-content:center;
 overflow:hidden;padding:.3mm 4mm .6mm}
.bottom img{width:104mm;height:11mm;object-fit:fill}
.code-text{font-size:11pt;line-height:4mm;letter-spacing:.1mm;font-weight:900;text-align:center;max-width:110mm;overflow:hidden}
.missing-code{height:11mm;display:flex;align-items:center;justify-content:center;font-size:10pt;font-weight:700;text-align:center}
@media print{
 html,body{background:#fff;margin:0;padding:0}
 .tools{display:none!important}
 .sheet{margin:0;border:1mm solid #000;width:120mm;height:75mm}
}
</style></head><body>
<div class="tools"><button type="button" onclick="window.print()">Печатать · 75×120 мм</button>
<p>Только ячейки карты склада. Выбери в драйвере горизонтальную этикетку 120×75 мм,
 масштаб 100%, поля 0, без колонтитулов. Этикеток: """ + str(len(rows)) + """</p>
<p>QR кодирует постоянный адрес пополнения. Code128 кодирует подтверждённый штрихкод товара из каталога МойСклад. Под ним указан артикул. При смене товара перепечатай этикетку.</p></div>
""" + sections + "</body></html>"
    return Response(page, mimetype="text/html",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
