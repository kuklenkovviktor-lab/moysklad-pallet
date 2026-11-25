"""Real PostgreSQL 17 race tests; ONLY in a disposable synthetic database."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from line_printer_pg_trial import (
    initialize_synthetic_pg,
    reserve_synthetic_pg,
)

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("PG trial requires explicit synthetic gate")
dsn = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not dsn.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Refusing unapproved synthetic PG URL")


def connect():
    return psycopg.connect(dsn)


def eligible(conn):
    # Synthetic marker, deliberately no real WB status or live catalog.
    return conn.execute("SELECT 1").fetchone()[0] == 1


class PGPrinterTrialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as conn:
            initialize_synthetic_pg(conn)

    def setUp(self):
        with connect() as conn:
            conn.execute("DELETE FROM synthetic_line_printer_claims")

    def reserve(self, *, order="SYNTH-ORDER-1", printer="P1",
                employee="EMP1", request="REQ1", account="test-account",
                validator=eligible):
        return reserve_synthetic_pg(
            connect, account_id=account, order_id=order,
            printer_id=printer, employee_id=employee,
            request_id=request, is_eligible_locked=validator)

    def rows(self):
        with connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM synthetic_line_printer_claims").fetchone()[0]

    def test_single_reservation_and_idempotency(self):
        self.assertEqual(self.reserve().reason, "RESERVED")
        self.assertEqual(self.reserve().reason, "ALREADY_RESERVED_SAME_REQUEST")
        self.assertEqual(self.rows(), 1)

    def test_competing_eight_terminals_one_winner(self):
        def actor(i):
            return self.reserve(printer=f"P{i}", employee=f"E{i}", request=f"R{i}")
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(actor, range(8)))
        self.assertEqual(sum(r.reason == "RESERVED" for r in results), 1)
        self.assertEqual(sum(r.reason == "ORDER_RESERVED_ELSEWHERE" for r in results), 7)
        self.assertEqual(self.rows(), 1)

    def test_cross_account_order_namespace(self):
        self.assertTrue(self.reserve().accepted)
        self.assertTrue(self.reserve(account="other", request="REQ2").accepted)
        self.assertEqual(self.rows(), 2)

    def test_duplicate_request_id_across_orders_fails_closed(self):
        self.assertTrue(self.reserve().accepted)
        self.assertEqual(self.reserve(order="other").reason, "REQUEST_ID_REUSED")
        self.assertEqual(self.rows(), 1)

    def test_failed_validator_writes_nothing(self):
        self.assertEqual(self.reserve(validator=lambda conn: False).reason,
                         "ORDER_INELIGIBLE")
        self.assertEqual(self.rows(), 0)

    def test_eligibility_exception_rolls_back(self):
        def fail(conn):
            raise RuntimeError("synthetic validation failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic validation failure"):
            self.reserve(validator=fail)
        self.assertEqual(self.rows(), 0)

    def test_bad_inputs_do_not_write(self):
        self.assertEqual(self.reserve(order="").reason, "INVALID_INPUT")
        self.assertEqual(self.reserve(printer="").reason, "INVALID_INPUT")
        self.assertEqual(self.rows(), 0)

    def test_rejects_without_explicit_gate(self):
        previous = os.environ.pop("GRIT_SYNTHETIC_PG_TEST", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "gate"):
                self.reserve()
        finally:
            if previous is not None:
                os.environ["GRIT_SYNTHETIC_PG_TEST"] = previous
        self.assertEqual(self.rows(), 0)


if __name__ == "__main__":
    unittest.main()
