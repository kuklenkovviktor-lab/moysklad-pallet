"""Disposable PG17 only: synthetic integration tests against GRIT table shapes."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from line_printer_pg_trial import initialize_synthetic_pg
from line_printer_pg_integrated_trial import reserve_integrated_synthetic
from line_printer_registry import create_printer

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Requires disposable PG synthetic test")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing unapproved PG test DSN")


def connect():
    return psycopg.connect(DSN)


TABLES = (
    "CREATE TABLE IF NOT EXISTS employees(id text primary key,active int not null)",
    "CREATE TABLE IF NOT EXISTS shifts(id text primary key,employee_id text not null,ended_at text)",
    "CREATE TABLE IF NOT EXISTS activities(id text primary key,shift_id text,employee_id text,activity_type text,ended_at text)",
    "CREATE TABLE IF NOT EXISTS grit_line_printers(printer_id text primary key,name text,lines_json text,token_digest text,enabled int)",
    "CREATE TABLE IF NOT EXISTS wb_fbs_orders(account_id text,order_id text,wb_status text,local_status text,primary key(account_id,order_id))",
    "CREATE TABLE IF NOT EXISTS wb_fbs_picking_queue(account_id text,order_id text,primary key(account_id,order_id))",
    "CREATE TABLE IF NOT EXISTS wb_fbs_order_skus(account_id text,order_id text,sku text,primary key(account_id,order_id,sku))",
    "CREATE TABLE IF NOT EXISTS fbs_catalog_barcodes(barcode text primary key,product_id text,quantity double precision,is_pack int)",
    "CREATE TABLE IF NOT EXISTS replenishment_locations(id text primary key,label text,active int)",
    "CREATE TABLE IF NOT EXISTS cell_sku_assignments(location_id text primary key,product_id text)"
)


class IntegratedPrinterPGTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as c:
            initialize_synthetic_pg(c)
            for stmt in TABLES:
                c.execute(stmt)

    def setUp(self):
        self.printer, self.payload = create_printer("p1", "Линии А-Б", ["А", "Б"])
        with connect() as c:
            for table in (
                "synthetic_line_printer_claims", "cell_sku_assignments",
                "replenishment_locations", "fbs_catalog_barcodes",
                "wb_fbs_order_skus", "wb_fbs_picking_queue", "wb_fbs_orders",
                "grit_line_printers", "activities", "shifts", "employees"
            ):
                c.execute("DELETE FROM " + table)
            c.execute("INSERT INTO employees VALUES ('e1',1),('e2',1)")
            c.execute("INSERT INTO shifts VALUES ('s1','e1',NULL),('s2','e2',NULL)")
            c.execute("""INSERT INTO activities VALUES
                ('a1','s1','e1','picking',NULL),
                ('a2','s2','e2','picking',NULL)""")
            c.execute("""
                INSERT INTO grit_line_printers VALUES (%s,%s,%s,%s,1)
            """, (self.printer.printer_id, self.printer.name, '["А","Б"]',
                  self.printer.token_digest))
            c.execute("""
                INSERT INTO wb_fbs_orders VALUES
                ('synthetic','o1','confirm','pending')
            """)
            c.execute("INSERT INTO wb_fbs_picking_queue VALUES ('synthetic','o1')")
            c.execute("INSERT INTO wb_fbs_order_skus VALUES ('synthetic','o1','SKU1')")
            c.execute("INSERT INTO fbs_catalog_barcodes VALUES ('SKU1','product1',1,0)")
            c.execute("INSERT INTO replenishment_locations VALUES ('loc1','А-05-1',1)")
            c.execute("INSERT INTO cell_sku_assignments VALUES ('loc1','product1')")

    def claim(self, *, employee_id="e1", shift_id="s1", printer_id="p1",
              qr_payload=None, request_id="req1"):
        return reserve_integrated_synthetic(
            connect, account_id="synthetic", order_id="o1",
            employee_id=employee_id, shift_id=shift_id,
            printer_id=printer_id,
            qr_payload=self.payload if qr_payload is None else qr_payload,
            request_id=request_id)

    def count(self):
        with connect() as c:
            return c.execute("SELECT count(*) FROM synthetic_line_printer_claims").fetchone()[0]

    def update(self, sql, args=()):
        with connect() as c:
            c.execute(sql, args)

    def test_valid_full_path_first_reservation(self):
        result, reason = self.claim()
        self.assertTrue(result.accepted)
        self.assertEqual(reason, "RESERVED")
        self.assertEqual(self.count(), 1)

    def test_closed_shift_denied(self):
        self.update("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
        self.assertEqual(self.claim()[1], "SHIFT_NOT_ACTIVE")
        self.assertEqual(self.count(), 0)

    def test_inactive_employee_denied(self):
        self.update("UPDATE employees SET active=0 WHERE id='e1'")
        self.assertEqual(self.claim()[1], "EMPLOYEE_INACTIVE")
        self.assertEqual(self.count(), 0)

    def test_wrong_shift_owner_denied(self):
        self.assertEqual(self.claim(shift_id="s2")[1], "SHIFT_NOT_ACTIVE")

    def test_picking_activity_required(self):
        self.update("UPDATE activities SET ended_at='closed' WHERE shift_id='s1'")
        self.assertEqual(self.claim()[1], "PICKING_NOT_ACTIVE")

    def test_wrong_or_revoked_printer_qr_denied(self):
        self.assertEqual(self.claim(qr_payload="GRIT-PRINTER:1:p1:invalid")[1], "QR_INVALID")
        self.update("UPDATE grit_line_printers SET enabled=0")
        self.assertEqual(self.claim()[1], "PRINTER_DISABLED")
        self.assertEqual(self.count(), 0)

    def test_completed_order_denied(self):
        self.update("UPDATE wb_fbs_orders SET local_status='complete'")
        self.assertEqual(self.claim()[1], "ORDER_NOT_PENDING")
        self.assertEqual(self.count(), 0)

    def test_cancelled_order_denied(self):
        self.update("UPDATE wb_fbs_orders SET wb_status='cancel'")
        self.assertEqual(self.claim()[1], "WB_STATUS_NOT_PICKABLE")

    def test_missing_queue_or_sku_denied(self):
        self.update("DELETE FROM wb_fbs_picking_queue")
        self.assertEqual(self.claim()[1], "ORDER_NOT_IN_QUEUE")
        self.update("INSERT INTO wb_fbs_picking_queue VALUES ('synthetic','o1')")
        self.update("DELETE FROM wb_fbs_order_skus")
        self.assertEqual(self.claim()[1], "ORDER_SKUS_MISSING")

    def test_unverified_catalog_product_or_inactive_cell_denied(self):
        self.update("UPDATE fbs_catalog_barcodes SET is_pack=1")
        self.assertEqual(self.claim()[1], "CATALOG_UNIT_BARCODE_MISSING")
        self.update("UPDATE fbs_catalog_barcodes SET is_pack=0")
        self.update("UPDATE replenishment_locations SET active=0")
        self.assertEqual(self.claim()[1], "PRODUCT_LOCATION_UNVERIFIED")

    def test_wrong_line_denied(self):
        self.update("UPDATE replenishment_locations SET label='В-05-1'")
        self.assertEqual(self.claim()[1], "PRINTER_DOES_NOT_COVER_ORDER")

    def test_multiple_lines_ambiguous(self):
        self.update("INSERT INTO replenishment_locations VALUES ('loc2','В-02',1)")
        self.update("INSERT INTO cell_sku_assignments VALUES ('loc2','product1')")
        self.assertEqual(self.claim()[1], "PRODUCT_MULTIPLE_LINES")

    def test_same_request_replay_while_valid_is_idempotent(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        result, reason = self.claim()
        self.assertTrue(result.accepted)
        self.assertEqual(reason, "ALREADY_RESERVED_SAME_REQUEST")
        self.assertEqual(self.count(), 1)

    def test_replay_after_shift_closes_is_denied_without_deleting_claim(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        self.update("UPDATE shifts SET ended_at='closed' WHERE id='s1'")
        result, reason = self.claim()
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "SHIFT_NOT_ACTIVE")
        self.assertEqual(self.count(), 1)

    def test_replay_after_order_cancel_is_denied_without_deleting_claim(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        self.update("UPDATE wb_fbs_orders SET wb_status='cancel'")
        result, reason = self.claim()
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "WB_STATUS_NOT_PICKABLE")
        self.assertEqual(self.count(), 1)

    def test_replay_after_printer_disabled_is_denied(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        self.update("UPDATE grit_line_printers SET enabled=0")
        result, reason = self.claim()
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "PRINTER_DISABLED")
        self.assertEqual(self.count(), 1)

    def test_replay_after_qr_rotation_is_denied(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        other, payload = create_printer("p1", "Линии А-Б", ["А", "Б"])
        self.update("UPDATE grit_line_printers SET token_digest=%s",
                    (other.token_digest,))
        result, reason = self.claim()
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "QR_INVALID")
        self.assertEqual(self.count(), 1)

    def test_replay_after_employee_deactivated_is_denied(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        self.update("UPDATE employees SET active=0 WHERE id='e1'")
        result, reason = self.claim()
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "EMPLOYEE_INACTIVE")
        self.assertEqual(self.count(), 1)

    def test_replay_with_other_employee_cannot_take_over(self):
        self.assertEqual(self.claim()[1], "RESERVED")
        result, reason = self.claim(employee_id="e2", shift_id="s2")
        self.assertFalse(result.accepted)
        self.assertEqual(reason, "ORDER_RESERVED_ELSEWHERE")
        self.assertEqual(self.count(), 1)

    def test_eight_terminals_compete_one_wins(self):
        def actor(i):
            employee = "e1" if i % 2 == 0 else "e2"
            shift = "s1" if i % 2 == 0 else "s2"
            return self.claim(employee_id=employee, shift_id=shift,
                              request_id=f"request-{i}")[0].reason
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(actor, range(8)))
        self.assertEqual(results.count("RESERVED"), 1)
        self.assertEqual(results.count("ORDER_RESERVED_ELSEWHERE"), 7)
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
