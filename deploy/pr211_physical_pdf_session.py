"""One physical synthetic PDF label via REAL PR211 HTTP+PG17 chain.

Runs ONLY inside a disposable Docker app container; no WB API/client,
production DB or real employee data. Prints ONE JSON claim to stdout and
accepts exactly one Windows spooler acknowledgment on stdin.
"""
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import os
import secrets
import sys

import fitz
import psycopg
from flask import Flask, session

from fbs_pdf_labels import parse_wb_labels
from services.printing.wb_original_pg17_outbox import initialize as queue_init
from services.printing.wb_original_pg17_device_auth import initialize as device_init
from services.printing.wb_original_print_http_synthetic import (
    make_isolated_original_print_blueprint,
)
from services.printing.wb_original_usb_agent_synthetic import decode_claim

TEST_ORDER='SYNTH-PR211-PDF-PHYSICAL-1'
TEST_SKU='2050291804984'
TEST_ARTICLE='SYNTH-ART'
TEST_STICKER='59298125228'
PRINTER='p1'
KEY=b'GRIT-PR211-PHYSICAL-SYNTH-32-KEY'
# Genuine Code128 B/C stripe decomposition for the INVENTED numeric label
# generated offline with code128 encoder. NOT a real WB label or WB API barcode.
CODE128_PATTERN='BaAbCbCcBaAaCbBbAaAbAaDbCbAaBbBbCaAbAaDaCaCaAbBbBbBaBbBcCaAaB'


def must_isolated():
    target=os.environ.get('PR211_SYNTHETIC_DATABASE_URL','')
    if not (os.environ.get('GRIT_SYNTHETIC_PG_TEST')=='YES_ISOLATED'
            and target.startswith(
              'postgresql://postgres:synthetic-only@pr211-db:5432/grit_pr211_synthetic_')
            and not os.environ.get('DATABASE_URL')):
        raise RuntimeError('ONLY_DISPOSABLE_PR211_DB_ALLOWED')
    return target


def pdf_bytes():
    doc=fitz.open()
    product=doc.new_page(width=315,height=420)
    for i,line in enumerate(('1 шт.','Артикул:',TEST_ARTICLE,
                              'Артикул WB: 123456789','Баркод: '+TEST_SKU)):
        product.insert_text((20,35+i*26),line,fontsize=12,fontname='china-s')
    sticker=doc.new_page(width=58*72/25.4,height=40*72/25.4)
    sticker.insert_text((8,13),'WB '+TEST_STICKER,fontsize=9,fontname='helv')
    # Actual machine-readable synthetic Code128 stripes matching printed 11-digit code.
    x=10.0
    for stripe in CODE128_PATTERN:
        width=(ord(stripe.lower())-96)*1.22
        if stripe.isupper():
            sticker.draw_rect(fitz.Rect(x,26,x+width,64),
                              color=(0,0,0),fill=(0,0,0),width=0)
        x+=width
    # Render a visible synthetic watermark as an IMAGE; strict sticker parser
    # still sees only one authentic-formatted 'WB ###########' text line.
    notice=fitz.open()
    p=notice.new_page(width=240,height=50)
    p.insert_text((8,22),'GRIT SYNTHETIC',fontsize=18,fontname='hebo')
    p.insert_text((8,42),'NOT WB  /  TEST ONLY',fontsize=12,fontname='hebo')
    watermark=p.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False).tobytes('png')
    notice.close()
    sticker.insert_image(fitz.Rect(10,73,150,109),stream=watermark)
    out=doc.tobytes()
    doc.close()
    check=fitz.open(stream=out,filetype='pdf')
    parsed=parse_wb_labels(check)  # REAL strict parser; NO mocks
    check.close()
    if (not parsed or not parsed['valid_structure']
            or parsed['unique_sticker_barcodes']!=1
            or parsed['products'][0]['stickers'][0]['barcode']!=TEST_STICKER):
        raise RuntimeError('REAL_PDF_PARSER_DID_NOT_ACCEPT_SYNTHETIC_SOURCE')
    return out


def connection(dsn):
    return psycopg.connect(dsn,autocommit=True)


def require_http(resp, name, code=200):
    if resp.status_code!=code or not isinstance(resp.json,dict):
        # Emit only the server's fixed refusal reason; never print PDF,
        # device tokens, QR values or untrusted response bodies.
        payload=resp.get_json(silent=True)
        reason=payload.get('error','UNKNOWN') if isinstance(payload,dict) else 'UNKNOWN'
        allowed={'PDF_STICKER_NOT_UNIQUELY_LINKED_TO_WB_ORDER',
                 'PDF_STICKER_LAYOUT_UNVERIFIED','STALE_WB_ORDER_SNAPSHOT',
                 'CATALOG_PRODUCT_OR_PACK_MISMATCH','WRONG_PRINTER_LINE',
                 'CELL_UNVERIFIED_OR_AMBIGUOUS','SOURCE_PAGE_NOT_58X40_MM',
                 'PDF_PRODUCT_BARCODE_DIFFERS_FROM_WB_SKU',
                 'INVALID_ACCOUNT_ID','SERVER_VERIFIED_PRINTER_LINES_REQUIRED'}
        safe=reason if reason in allowed else 'UNSPECIFIED'
        raise RuntimeError(name+'_HTTP_'+str(resp.status_code)+'_'+safe)
    return resp.json


def main():
    dsn=must_isolated()
    if len(KEY)!=32:
        raise RuntimeError('KEY_SIZE_ERROR')
    now=datetime.now(timezone.utc).isoformat()
    qr='GRIT-PRINTER:1:p1:'+secrets.token_hex(24)
    with connection(dsn) as conn:
        for sql in (
          'CREATE TABLE wb_fbs_orders(account_id TEXT,order_id TEXT,wb_status TEXT,local_status TEXT,synced_at TEXT)',
          'CREATE TABLE wb_fbs_stickers(account_id TEXT,order_id TEXT,barcode TEXT)',
          'CREATE TABLE wb_fbs_picking_queue(account_id TEXT,order_id TEXT)',
          'CREATE TABLE wb_fbs_order_skus(account_id TEXT,order_id TEXT,sku TEXT)',
          'CREATE TABLE fbs_catalog_barcodes(barcode TEXT,product_id TEXT,article TEXT,quantity DOUBLE PRECISION,is_pack INTEGER)',
          'CREATE TABLE replenishment_locations(id TEXT,label TEXT,active INTEGER)',
          'CREATE TABLE cell_sku_assignments(location_id TEXT,product_id TEXT)',
          'CREATE TABLE grit_line_printers(printer_id TEXT PRIMARY KEY,name TEXT,lines_json TEXT,token_digest TEXT,enabled INTEGER)',
        ):
            conn.execute(sql)
        queue_init(conn)
        device_init(conn)
        conn.execute('INSERT INTO wb_fbs_orders VALUES(%s,%s,%s,%s,%s)',
                     ('synthetic',TEST_ORDER,'confirm','pending',now))
        conn.execute('INSERT INTO wb_fbs_stickers VALUES(%s,%s,%s)',
                     ('synthetic',TEST_ORDER,TEST_STICKER))
        conn.execute('INSERT INTO wb_fbs_picking_queue VALUES(%s,%s)',
                     ('synthetic',TEST_ORDER))
        conn.execute('INSERT INTO wb_fbs_order_skus VALUES(%s,%s,%s)',
                     ('synthetic',TEST_ORDER,TEST_SKU))
        conn.execute('INSERT INTO fbs_catalog_barcodes VALUES(%s,%s,%s,%s,%s)',
                     (TEST_SKU,'synthetic-product','SYNTH-ART',1,0))
        conn.execute('INSERT INTO replenishment_locations VALUES(%s,%s,%s)',
                     ('synthetic-cell','А-05-1',1))
        conn.execute('INSERT INTO cell_sku_assignments VALUES(%s,%s)',
                     ('synthetic-cell','synthetic-product'))
        conn.execute('INSERT INTO grit_line_printers VALUES(%s,%s,%s,%s,%s)',
                     (PRINTER,'Synthetic printer A','["А"]',
                      hashlib.sha256(qr.encode()).hexdigest(),1))

    app=Flask(__name__)
    app.secret_key=secrets.token_hex(32)
    def manager_guard():
        return session.get('manager_web') is True
    def worker_guard():
        if session.get('synthetic_worker') is not True:
            return None
        return {'employee_id':'SYNTH-BADGE-1',
                'shift_id':'SYNTH-SHIFT-1','picking_active':True}
    app.register_blueprint(make_isolated_original_print_blueprint(
        lambda:connection(dsn),key=KEY,
        manager_guard=manager_guard,worker_guard=worker_guard))
    client=app.test_client()
    with client.session_transaction() as sess:
        sess['manager_web']=True
    csrf=require_http(client.get('/synthetic/pr211/manager/session'),'MANAGER')['csrf']
    payload=pdf_bytes()
    stage=require_http(client.post('/synthetic/pr211/manager/pdf/stage',
        data={'csrf':csrf,'printer_id':PRINTER,
              'file':(io.BytesIO(payload),'synthetic-original-like.pdf')},
        content_type='multipart/form-data'),'MANAGER_PDF_STAGE')
    if stage.get('staged')!=1 or not stage.get('awaiting_worker_qr'):
        raise RuntimeError('PDF_DID_NOT_STAGE_EXACTLY_ONE')
    enrolled=require_http(client.post(
        '/synthetic/pr211/manager/devices/enroll',
        json={'csrf':csrf,'printer_id':PRINTER}),'DEVICE_ENROLL',201)
    headers={'Authorization':'Bearer '+enrolled['token'],
             'X-GRIT-Device-ID':enrolled['device_id']}
    before=require_http(client.post(
        '/synthetic/pr211/agent/claim',json={},headers=headers),'PRE_QR')
    if before.get('state')!='EMPTY':
        raise RuntimeError('UNAUTHORIZED_AUTO_PRINT_BEFORE_WORKER_QR')
    with client.session_transaction() as sess:
        sess.clear()
        sess['synthetic_worker']=True
    wcsrf=require_http(client.get('/synthetic/pr211/worker/session'),
                       'SYNTHETIC_SHIFT')['csrf']
    released=require_http(client.post('/synthetic/pr211/worker/printer-qr',
        json={'csrf':wcsrf,'qr':qr}),'SIGNED_QR')
    if released.get('released')!=1 or released.get('lines')!=['А']:
        raise RuntimeError('WORKER_QR_WRONG_LINE_OR_JOB_COUNT')
    claim=require_http(client.post(
        '/synthetic/pr211/agent/claim',json={},headers=headers),'DEVICE_CLAIM')
    verified=decode_claim(claim)
    if verified is None:
        raise RuntimeError('ORIGINAL_PDF_CLAIM_EMPTY')
    ticket,raw,digest=verified
    if raw.count(b'PRINT 1,1')!=1:
        raise RuntimeError('NOT_EXACTLY_ONE_TSPL_LABEL')
    print(json.dumps({'kind':'PR211_SYNTHETIC_PHYSICAL_PDF',
                      'order':TEST_ORDER,'sticker':TEST_STICKER,
                      'line':'А', 'claim':claim},separators=(',',':')),
          flush=True)
    # Claim committed to PG17; Windows must journal BEFORE RAW submission.
    # One ACK only. Lost SSH connection = no automatic retry.
    ack=sys.stdin.readline().strip()
    if ack not in ('SUBMITTED','UNKNOWN'):
        ack='UNKNOWN'
    result=client.post('/synthetic/pr211/agent/report',
        json={'ticket_id':ticket,'result':ack},headers=headers)
    if result.status_code!=200:
        print('PR211_SERVER_REPORT_FAILED_NO_RETRY',file=sys.stderr,flush=True)
        raise RuntimeError('SERVER_REPORT_UNCONFIRMED_NO_RETRY')
    print('PR211_SERVER_REPORT_'+result.json['state'],flush=True)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        print('PR211_PHYSICAL_SYNTHETIC_PREP_FAILED: '+str(exc),
              file=sys.stderr,flush=True)
        sys.exit(1)
