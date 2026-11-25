"""Synthetic concurrency tests of the isolated reservation reference model."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
import unittest
from pathlib import Path

from line_printer_reservations import (
    get_synthetic_reservation,
    initialize_synthetic_schema,
    reserve_synthetic,
)


class PrinterReservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = str(Path(temp.name) / "reservations.db")
        with sqlite3.connect(self.path) as conn:
            initialize_synthetic_schema(conn)

    def claim(self, order_id="ORDER-1", printer_id="P1",
              employee_id="EMP-1", reservation_id="REQUEST-1",
              account_id="synthetic-account", eligible=True):
        return reserve_synthetic(
            self.path, account_id=account_id, order_id=order_id,
            printer_id=printer_id, employee_id=employee_id,
            reservation_id=reservation_id, is_eligible=lambda: eligible)

    def count(self):
        with sqlite3.connect(self.path) as db:
            return db.execute("SELECT COUNT(*) FROM synthetic_printer_reservations").fetchone()[0]

    def test_first_claim_reserves_exactly_once(self):
        r = self.claim()
        self.assertTrue(r.accepted)
        self.assertEqual(r.reason, "RESERVED")
        self.assertEqual(get_synthetic_reservation(
            self.path, "synthetic-account", "ORDER-1"),
            ("REQUEST-1", "P1", "EMP-1"))
        self.assertEqual(self.count(), 1)

    def test_same_request_is_idempotent(self):
        self.assertTrue(self.claim().accepted)
        r = self.claim(eligible=False)
        self.assertTrue(r.accepted)
        self.assertEqual(r.reason, "ALREADY_RESERVED_SAME_REQUEST")
        self.assertEqual(self.count(), 1)

    def test_other_printer_or_employee_cannot_claim(self):
        self.claim()
        for args in (
            {"printer_id": "P2", "reservation_id": "REQUEST-2"},
            {"employee_id": "EMP-2", "reservation_id": "REQUEST-3"},
        ):
            with self.subTest(args=args):
                r = self.claim(**args)
                self.assertFalse(r.accepted)
                self.assertEqual(r.reason, "ORDER_RESERVED_ELSEWHERE")
        self.assertEqual(self.count(), 1)

    def test_reuse_request_id_for_other_order_is_blocked(self):
        self.claim()
        r = self.claim(order_id="ORDER-2")
        self.assertFalse(r.accepted)
        self.assertEqual(r.reason, "REQUEST_ID_REUSED")
        self.assertEqual(self.count(), 1)

    def test_ineligible_and_invalid_requests_do_not_write(self):
        self.assertEqual(self.claim(eligible=False).reason, "ORDER_INELIGIBLE")
        self.assertEqual(self.claim(order_id="").reason, "INVALID_INPUT")
        self.assertEqual(self.count(), 0)

    def test_two_printers_racing_for_same_order_only_one_wins(self):
        def worker(i):
            return self.claim(
                printer_id=f"P{i}", employee_id=f"EMP-{i}",
                reservation_id=f"REQUEST-{i}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))
        self.assertEqual(sum(r.accepted for r in results), 1)
        self.assertEqual(sum(r.reason == "ORDER_RESERVED_ELSEWHERE"
                             for r in results), 7)
        self.assertEqual(self.count(), 1)

    def test_different_accounts_have_distinct_order_namespace(self):
        self.assertTrue(self.claim().accepted)
        self.assertTrue(self.claim(account_id="another-account",
                                   reservation_id="REQUEST-2").accepted)
        self.assertEqual(self.count(), 2)

    def test_bad_eligibility_raises_and_rolls_back(self):
        def fail():
            raise ValueError("synthetic failure")
        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            reserve_synthetic(
                self.path, account_id="a", order_id="o", printer_id="p",
                employee_id="e", reservation_id="r", is_eligible=fail)
        self.assertEqual(self.count(), 0)
        self.assertTrue(self.claim().accepted)


if __name__ == "__main__":
    unittest.main()
