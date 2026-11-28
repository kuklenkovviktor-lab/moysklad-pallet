"""Synthetic PDF source-to-label routing tests. No physical USB, no WB access."""
from datetime import datetime, timezone
from unittest import TestCase, main
from unittest.mock import patch
import sqlite3
import fitz

from services.printing.wb_pdf_original_label import (
    LabelSourceError, match_source_pdf, source_page_to_tspl
)

NOW=datetime(2026,11,27,14,0,tzinfo=timezone.utc)


def source_pdf():
    doc=fitz.open()
    page=doc.new_page(width=58*72/25.4,height=40*72/25.4)
    page.insert_text((20,30),"WB 59298125228",fontsize=12)
    data=doc.tobytes()
    doc.close()
    return data


def parsed():
    return {"valid_structure":True,"products":[{
        "article":"SYNTH_ARTICLE","product_barcode":"2050291804984",
        "stickers":[{"barcode":"59298125228","page":1}]}]}


class OriginalWBSourceTests(TestCase):
    def setUp(self):
        self.db=sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE wb_fbs_orders (account_id TEXT,order_id TEXT,
                wb_status TEXT,local_status TEXT,synced_at TEXT);
            CREATE TABLE wb_fbs_stickers (account_id TEXT,order_id TEXT,barcode TEXT);
            CREATE TABLE wb_fbs_picking_queue (account_id TEXT,order_id TEXT);
            CREATE TABLE wb_fbs_order_skus (account_id TEXT,order_id TEXT,sku TEXT);
            CREATE TABLE fbs_catalog_barcodes (
                barcode TEXT,product_id TEXT,article TEXT,quantity REAL,is_pack INTEGER);
            CREATE TABLE replenishment_locations (id TEXT,label TEXT,active INTEGER);
            CREATE TABLE cell_sku_assignments (location_id TEXT,product_id TEXT);
        """)
        self.db.execute("INSERT INTO wb_fbs_orders VALUES(?,?,?,?,?)",
                        ("synthetic","o1","confirm","pending",NOW.isoformat()))
        self.db.execute("INSERT INTO wb_fbs_stickers VALUES(?,?,?)",
                        ("synthetic","o1","59298125228"))
        self.db.execute("INSERT INTO wb_fbs_picking_queue VALUES(?,?)",
                        ("synthetic","o1"))
        self.db.execute("INSERT INTO wb_fbs_order_skus VALUES(?,?,?)",
                        ("synthetic","o1","2050291804984"))
        self.db.execute("INSERT INTO fbs_catalog_barcodes VALUES(?,?,?,?,?)",
                        ("2050291804984","p1","SYNTH_ARTICLE",1,0))
        self.db.execute("INSERT INTO replenishment_locations VALUES(?,?,?)",
                        ("loc1","А-05-1",1))
        self.db.execute("INSERT INTO cell_sku_assignments VALUES(?,?)",("loc1","p1"))
        self.data=source_pdf()

    def mapped(self):
        before=self.db.total_changes
        with patch("services.printing.wb_pdf_original_label.parse_wb_labels",
                   return_value=parsed()):
            result=match_source_pdf(self.data,self.db,account_id="synthetic",
                                    verified_lines=("А",),clock=NOW)
        self.assertEqual(before,self.db.total_changes,"Cannot mutate order cache")
        return result

    def rejects(self, reason):
        with self.assertRaisesRegex(LabelSourceError,reason):
            self.mapped()

    def test_one_exact_order_pdf_sticker_and_line(self):
        jobs=self.mapped()
        self.assertEqual(len(jobs),1)
        self.assertEqual((jobs[0].order_id,jobs[0].line,jobs[0].page_index),
                         ("o1","А",0))

    def test_original_pdf_raster_bitmap_not_fake_barcode(self):
        raw=source_page_to_tspl(self.data,0)
        self.assertIn(b'BITMAP 0,0,58,320,0,',raw)
        self.assertIn(b'SIZE 58 mm,40 mm',raw)
        self.assertTrue(raw.endswith(b'PRINT 1,1\r\n'))
        self.assertEqual(len(raw),18644)
        self.assertNotIn(b'GRIT-DEMO-',raw)
        self.assertNotIn(b'NOT WB',raw)

    def test_missing_barcode_source_rejected(self):
        self.db.execute("DELETE FROM wb_fbs_stickers")
        self.rejects("PDF_STICKER_NOT_UNIQUELY_LINKED")

    def test_ambiguous_sticker_across_orders_rejected(self):
        self.db.execute("INSERT INTO wb_fbs_orders VALUES(?,?,?,?,?)",
                        ("synthetic","o2","confirm","pending",NOW.isoformat()))
        self.db.execute("INSERT INTO wb_fbs_stickers VALUES(?,?,?)",
                        ("synthetic","o2","59298125228"))
        self.db.execute("INSERT INTO wb_fbs_picking_queue VALUES(?,?)",
                        ("synthetic","o2"))
        self.rejects("PDF_STICKER_NOT_UNIQUELY_LINKED")

    def test_cancelled_order_rejected(self):
        self.db.execute("UPDATE wb_fbs_orders SET wb_status='canceled'")
        self.rejects("DUPLICATE_OR_BLOCKED_ORDER")

    def test_stale_order_rejected(self):
        self.db.execute("UPDATE wb_fbs_orders SET synced_at='2026-10-01T12:00:00+00:00'")
        self.rejects("STALE_WB_ORDER_SNAPSHOT")

    def test_mismatched_original_product_barcode_rejected(self):
        self.db.execute("UPDATE wb_fbs_order_skus SET sku='wrong'")
        self.rejects("PDF_PRODUCT_BARCODE_DIFFERS_FROM_WB_SKU")

    def test_wrong_line_rejected(self):
        self.db.execute("UPDATE replenishment_locations SET label='В-02-1'")
        self.rejects("WRONG_PRINTER_LINE")

    def test_duplicate_cell_rejected(self):
        self.db.execute("INSERT INTO replenishment_locations VALUES('loc2','Б-02-1',1)")
        self.db.execute("INSERT INTO cell_sku_assignments VALUES('loc2','p1')")
        self.rejects("CELL_UNVERIFIED_OR_AMBIGUOUS")

    def test_pack_rejected(self):
        self.db.execute("UPDATE fbs_catalog_barcodes SET is_pack=1,quantity=4")
        self.rejects("CATALOG_PRODUCT_OR_PACK_MISMATCH")

    def test_bad_pdf_and_non58x40_page_rejected(self):
        with self.assertRaises(LabelSourceError):
            source_page_to_tspl(b'not pdf',0)
        with self.assertRaises(LabelSourceError):
            source_page_to_tspl(self.data,1)
        doc=fitz.open()
        doc.new_page(width=210,height=297)
        bad=doc.tobytes()
        doc.close()
        with self.assertRaisesRegex(LabelSourceError,'SOURCE_PAGE_NOT_58X40_MM'):
            source_page_to_tspl(bad,0)

    def test_invalid_parser_and_duplicate_page_rejected(self):
        with patch("services.printing.wb_pdf_original_label.parse_wb_labels",
                   return_value={"valid_structure":False,"products":[]}):
            with self.assertRaisesRegex(LabelSourceError,'PDF_STICKER_LAYOUT_UNVERIFIED'):
                match_source_pdf(self.data,self.db,account_id="synthetic",
                                 verified_lines=("А",),clock=NOW)
        p=parsed()
        p['products'][0]['stickers'].append({"barcode":"59298125228","page":1})
        with patch("services.printing.wb_pdf_original_label.parse_wb_labels",
                   return_value=p):
            with self.assertRaisesRegex(LabelSourceError,'DUPLICATE_OR_BLOCKED_ORDER'):
                match_source_pdf(self.data,self.db,account_id="synthetic",
                                 verified_lines=("А",),clock=NOW)


if __name__=="__main__":
    main()
