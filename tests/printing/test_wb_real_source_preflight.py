"""Read-only contract tests against real GRIT WB v11/catalog/cell SQL schema.

All records invented. No external DB, no requests, no USB, no WB API.
"""
import sqlite3
import unittest
from datetime import datetime, timezone

from services.printing.wb_fbs_real_source_preflight import select_wb_fbs_candidates

NOW = datetime(2026, 11, 27, 14, 0, tzinfo=timezone.utc)


class SourcePreflightTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE wb_fbs_orders (
                account_id TEXT, order_id TEXT, wb_status TEXT,
                local_status TEXT, synced_at TEXT
            );
            CREATE TABLE wb_fbs_picking_queue (account_id TEXT,order_id TEXT);
            CREATE TABLE wb_fbs_stickers (account_id TEXT,order_id TEXT,
                                         barcode TEXT, format TEXT);
            CREATE TABLE wb_fbs_order_skus (account_id TEXT,order_id TEXT,sku TEXT);
            CREATE TABLE fbs_catalog_barcodes (
                barcode TEXT,product_id TEXT,quantity REAL,is_pack INTEGER
            );
            CREATE TABLE replenishment_locations (id TEXT,label TEXT,active INTEGER);
            CREATE TABLE cell_sku_assignments (location_id TEXT,product_id TEXT);
        """)
        self.db.execute("INSERT INTO wb_fbs_orders VALUES(?,?,?,?,?)",
                        ("a1","o1","confirm","pending",NOW.isoformat()))
        self.db.execute("INSERT INTO wb_fbs_picking_queue VALUES(?,?)",("a1","o1"))
        self.db.execute("INSERT INTO wb_fbs_stickers VALUES(?,?,?,?)",
                        ("a1","o1","SYNTHETIC-NOT-A-REAL-LABEL","test"))
        self.db.execute("INSERT INTO wb_fbs_order_skus VALUES(?,?,?)",("a1","o1","SYNTH-SKU"))
        self.db.execute("INSERT INTO fbs_catalog_barcodes VALUES(?,?,?,?)",
                        ("SYNTH-SKU","product-1",1.0,0))
        self.db.execute("INSERT INTO replenishment_locations VALUES(?,?,?)",
                        ("loc1","А-05-1",1))
        self.db.execute("INSERT INTO cell_sku_assignments VALUES(?,?)",
                        ("loc1","product-1"))
        self.db.commit()

    def check(self, decision, **kwargs):
        before = self.db.total_changes
        result = select_wb_fbs_candidates(
            self.db,printer_lines=("А","Б"),clock=NOW,**kwargs)
        self.assertEqual(self.db.total_changes,before,"preflight must not write")
        self.assertEqual(len(result),1)
        self.assertEqual(result[0].decision,decision)
        self.assertFalse(hasattr(result[0],"barcode"))
        self.assertFalse(hasattr(result[0],"sticker"))
        return result[0]

    def test_real_schema_mapping_routable_not_a_printable_label(self):
        row = self.check("ROUTABLE_LABEL_SOURCE_UNVERIFIED")
        self.assertEqual((row.account_id,row.order_id,row.line,row.product_id),
                         ("a1","o1","А","product-1"))

    def test_missing_sticker(self):
        self.db.execute("DELETE FROM wb_fbs_stickers")
        self.check("ORIGINAL_STICKER_ABSENT")

    def test_old_synced_order_fails_closed(self):
        self.db.execute("UPDATE wb_fbs_orders SET synced_at='2026-10-01T01:00:00+00:00'")
        self.check("STALE_OR_FUTURE_WB_SNAPSHOT")

    def test_cancelled_order_blocked(self):
        self.db.execute("UPDATE wb_fbs_orders SET wb_status='canceled'")
        self.check("STATUS_NOT_PICKABLE")

    def test_missing_sku(self):
        self.db.execute("DELETE FROM wb_fbs_order_skus")
        self.check("SKU_MISSING")

    def test_pack_not_single_unit(self):
        self.db.execute("UPDATE fbs_catalog_barcodes SET quantity=4,is_pack=1")
        self.check("PACK_OR_INVALID_UNIT")

    def test_ambiguous_multiple_cells(self):
        self.db.execute("INSERT INTO replenishment_locations VALUES('loc2','Б-02-1',1)")
        self.db.execute("INSERT INTO cell_sku_assignments VALUES('loc2','product-1')")
        self.check("CELL_MISSING_AMBIGUOUS_OR_INACTIVE")

    def test_inactive_cell(self):
        self.db.execute("UPDATE replenishment_locations SET active=0")
        self.check("CELL_MISSING_AMBIGUOUS_OR_INACTIVE")

    def test_wrong_line(self):
        self.db.execute("UPDATE replenishment_locations SET label='В-01-1'")
        self.check("PRINTER_LINE_MISMATCH")

    def test_two_different_products(self):
        self.db.execute("INSERT INTO wb_fbs_order_skus VALUES('a1','o1','SYNTH-SKU-2')")
        self.db.execute("INSERT INTO fbs_catalog_barcodes VALUES('SYNTH-SKU-2','product-2',1,0)")
        self.check("MULTI_PRODUCT_ORDER_REQUIRES_REVIEW")

    def test_invalid_server_lines_fail_closed(self):
        for lines in ((),("А;DROP",),("a",),("А",""),"А"):
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    select_wb_fbs_candidates(self.db,printer_lines=lines,clock=NOW)

    def test_limit_and_no_printing_side_effects(self):
        for limit in (0,1001,-1,True):
            with self.assertRaises(ValueError):
                select_wb_fbs_candidates(
                    self.db,printer_lines=("А",),clock=NOW,max_orders=limit)
        self.assertEqual(self.db.execute("SELECT count(*) FROM wb_fbs_orders").fetchone()[0],1)


if __name__=="__main__":
    unittest.main()
