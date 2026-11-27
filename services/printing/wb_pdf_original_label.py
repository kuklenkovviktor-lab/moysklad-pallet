"""Original WB PDF page → verified order/line → original bitmap TSPL, OFFLINE ONLY.

No USB, APIs, HTTP endpoints, migrations or production registration. Caller must
separately authorize terminal and signed printer QR and use a durable atomic
claim. Never fabricate a WB sticker from its text barcode.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import fitz
from fbs_pdf_labels import parse_wb_labels
from wb_qr_codec import canonical_wb_sticker

MAX_BYTES = 25 * 1024 * 1024
MAX_PAGES = 10000
DOTS_X, DOTS_Y = 464, 320


class LabelSourceError(ValueError):
    pass


@dataclass(frozen=True)
class LabelJob:
    account_id: str
    order_id: str
    sticker_code: str
    article: str
    line: str
    page_index: int
    product_barcode: str = ""
    product_id: str = ""


def _rows(conn, statement, parameters=(), adapt=lambda q: q):
    cursor = conn.cursor()
    try:
        cursor.execute(adapt(statement), parameters)
        return cursor.fetchall()
    finally:
        cursor.close()


def _is_58x40(page):
    width_mm = page.rect.width * 25.4 / 72
    height_mm = page.rect.height * 25.4 / 72
    return abs(width_mm - 58) <= 1.5 and abs(height_mm - 40) <= 1.5


def match_source_pdf(pdf_bytes, conn, *, account_id, verified_lines,
                     clock=None, max_age_seconds=180, adapt=lambda q: q):
    """Read-only exact PDF sticker→WB order→catalog→one active cell mapping.

    Reject ENTIRE source if even one sticker is ambiguous. Supports only PDFs
    accepted by existing strict fbs_pdf_labels.parse_wb_labels.
    """
    if (not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b'%PDF-')
            or len(pdf_bytes) > MAX_BYTES):
        raise LabelSourceError('INVALID_OR_OVERSIZED_ORIGINAL_PDF')
    if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', account_id):
        raise LabelSourceError('INVALID_ACCOUNT_ID')
    if (not isinstance(verified_lines, (list,tuple,frozenset))
            or not verified_lines or len(verified_lines) > 20
            or any(not isinstance(v,str) or not re.fullmatch(r'[А-ЯЁA-Z]{1,3}', v)
                   for v in verified_lines)):
        raise LabelSourceError('SERVER_VERIFIED_PRINTER_LINES_REQUIRED')
    if type(max_age_seconds) is not int or not 30 <= max_age_seconds <= 86400:
        raise LabelSourceError('INVALID_FRESHNESS')
    clock = clock or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise LabelSourceError('CLOCK_MUST_BE_TIMEZONE_AWARE')
    try:
        doc = fitz.open(stream=pdf_bytes,filetype='pdf')
    except Exception as exc:
        raise LabelSourceError('BAD_PDF') from exc
    try:
        if doc.needs_pass or not 1 <= doc.page_count <= MAX_PAGES:
            raise LabelSourceError('UNSAFE_PDF')
        report = parse_wb_labels(doc)
        if not report or not report['valid_structure']:
            raise LabelSourceError('PDF_STICKER_LAYOUT_UNVERIFIED')
        # The actual WB FBS v11 API stores the 9-character QR ("*"+base64),
        # while original printed Code128 PDF pages often show 11 decimal digits.
        # Compare normalized semantic identity, NEVER replace PDF artwork.
        candidates = _rows(conn, """SELECT o.order_id,o.wb_status,o.local_status,
                    o.synced_at,s.barcode
                FROM wb_fbs_stickers s JOIN wb_fbs_orders o
                    ON o.account_id=s.account_id AND o.order_id=s.order_id
                JOIN wb_fbs_picking_queue q
                    ON q.account_id=o.account_id AND q.order_id=o.order_id
                WHERE o.account_id=?""", (account_id,), adapt)
        mapped = {}
        for candidate in candidates:
            canonical = canonical_wb_sticker(candidate[4])
            if isinstance(canonical,str) and re.fullmatch(r'\\d{11}',canonical):
                mapped.setdefault(canonical,[]).append(candidate)
        jobs, seen_orders = [], set()
        for product in report['products']:
            article = product['article']
            for label in product['stickers']:
                code = label['barcode']
                matched = mapped.get(code,[])
                if len(matched) != 1:
                    raise LabelSourceError('PDF_STICKER_NOT_UNIQUELY_LINKED_TO_WB_ORDER')
                order_id, wb_state, local_state, synchronized, saved_code = matched[0]
                if (order_id in seen_orders or wb_state != 'confirm'
                        or local_state != 'pending'
                        or canonical_wb_sticker(saved_code) != code):
                    raise LabelSourceError('DUPLICATE_OR_BLOCKED_ORDER')
                seen_orders.add(order_id)
                try:
                    stamp = datetime.fromisoformat(str(synchronized).replace('Z','+00:00'))
                    if stamp.tzinfo is None or stamp > clock or (clock-stamp).total_seconds() > max_age_seconds:
                        raise ValueError('stale')
                except (ValueError,TypeError,OverflowError) as exc:
                    raise LabelSourceError('STALE_WB_ORDER_SNAPSHOT') from exc
                sku_rows = _rows(conn, """SELECT sku FROM wb_fbs_order_skus
                    WHERE account_id=? AND order_id=?""", (account_id,order_id),adapt)
                if len(sku_rows) != 1:
                    raise LabelSourceError('UNSUPPORTED_MULTI_SKU_OR_MISSING_SKU')
                sku = sku_rows[0][0]
                if str(sku) != str(product['product_barcode']):
                    raise LabelSourceError('PDF_PRODUCT_BARCODE_DIFFERS_FROM_WB_SKU')
                catalog = _rows(conn, """SELECT product_id,article,quantity,is_pack
                    FROM fbs_catalog_barcodes WHERE barcode=?""",(sku,),adapt)
                if len(catalog) != 1:
                    raise LabelSourceError('CATALOG_MATCH_NOT_UNIQUE')
                product_id, catalog_article, quantity, is_pack = catalog[0]
                try:
                    unit = (bool(product_id) and float(quantity) == 1
                            and int(is_pack) == 0)
                except (ValueError,TypeError,OverflowError):
                    unit = False
                if not unit or str(catalog_article).strip().casefold() != article.strip().casefold():
                    raise LabelSourceError('CATALOG_PRODUCT_OR_PACK_MISMATCH')
                cells = _rows(conn, """SELECT l.label,l.active FROM cell_sku_assignments a
                    JOIN replenishment_locations l ON l.id=a.location_id
                    WHERE a.product_id=?""", (product_id,),adapt)
                try:
                    one_active = len(cells)==1 and int(cells[0][1])==1
                except (TypeError,ValueError,OverflowError):
                    one_active = False
                if not one_active:
                    raise LabelSourceError('CELL_UNVERIFIED_OR_AMBIGUOUS')
                found = re.match(r'^([А-ЯЁA-Z]{1,3})-?\d',str(cells[0][0]).upper())
                if not found or found.group(1) not in verified_lines:
                    raise LabelSourceError('WRONG_PRINTER_LINE')
                page_index = label['page']-1
                if not _is_58x40(doc[page_index]):
                    raise LabelSourceError('SOURCE_PAGE_NOT_58X40_MM')
                jobs.append(LabelJob(account_id,str(order_id),code,article,
                                     found.group(1),page_index,
                                     str(sku),str(product_id)))
        if not jobs:
            raise LabelSourceError('NO_VERIFIED_STICKERS')
        return tuple(jobs)
    finally:
        doc.close()


def source_page_to_tspl(pdf_bytes, page_index):
    """Keep original PDF page artwork as 1bit TSPL bitmap, 203dpi, 58×40.

    Intentionally no simulated WB design, text/barcode substitution, device IO
    or print confirmation. Result can ONLY be sent after separate auth/claim.
    """
    if (not isinstance(pdf_bytes,bytes) or not pdf_bytes.startswith(b'%PDF-')
            or len(pdf_bytes)>MAX_BYTES or type(page_index) is not int):
        raise LabelSourceError('INVALID_PRINT_SOURCE')
    try:
        doc=fitz.open(stream=pdf_bytes,filetype='pdf')
    except Exception as exc:
        raise LabelSourceError('BAD_PRINT_SOURCE_PDF') from exc
    try:
        if doc.needs_pass or doc.page_count>MAX_PAGES or not 0<=page_index<doc.page_count:
            raise LabelSourceError('INVALID_PRINT_PAGE')
        page=doc[page_index]
        if not _is_58x40(page):
            raise LabelSourceError('SOURCE_PAGE_NOT_58X40_MM')
        pix=page.get_pixmap(matrix=fitz.Matrix(DOTS_X/page.rect.width,
                                                DOTS_Y/page.rect.height),
                            colorspace=fitz.csGRAY,alpha=False)
        if pix.width!=DOTS_X or pix.height!=DOTS_Y or pix.n!=1:
            raise LabelSourceError('RENDERED_SIZE_MISMATCH')
        stride=DOTS_X//8
        rows=[]
        for y in range(DOTS_Y):
            pixels=pix.samples[y*pix.stride:y*pix.stride+DOTS_X]
            packed=bytearray(stride)
            for x,shade in enumerate(pixels):
                if shade<160:
                    packed[x//8]|=0x80>>(x%8)
            rows.append(bytes(packed))
        bitmap=b''.join(rows)
        if len(bitmap)!=18560:
            raise LabelSourceError('BITMAP_BYTE_COUNT_MISMATCH')
        return (b'SIZE 58 mm,40 mm\r\nGAP 2 mm,0 mm\r\nDIRECTION 1\r\n'
                b'CLS\r\nBITMAP 0,0,58,320,0,'+bitmap+b'\r\nPRINT 1,1\r\n')
    finally:
        doc.close()
