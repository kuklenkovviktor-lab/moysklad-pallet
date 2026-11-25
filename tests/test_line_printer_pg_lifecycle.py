"""Isolated PostgreSQL17 tests for four-state GRIT printer lifecycle."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from line_printer_pg_lifecycle import (
    initialize_lifecycle, reserve_lifecycle, complete_lifecycle,
    release_lifecycle, read_lifecycle,
)

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Synthetic gate required")
DSN = os.getenv("PR192_SYNTHETIC_DATABASE_URL", "")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Synthetic PostgreSQL URL required")


def connect():
    return psycopg.connect(DSN)


def grant(conn, action, previous, record):
    return True


def deny(conn, action, previous, record):
    return False


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as c:
            initialize_lifecycle(c)

    def setUp(self):
        with connect() as c:
            c.execute("DELETE FROM synthetic_line_printer_lifecycle_events")
            c.execute("DELETE FROM synthetic_line_printer_lifecycle")

    def reserve(self, *, order="order1", request="reserve1", reservation="lease1",
                printer="p1", employee="e1", actor="e1", authorize=grant):
        return reserve_lifecycle(
            connect, account_id="synthetic", order_id=order,
            action_id=request, actor_id=actor, reservation_id=reservation,
            printer_id=printer, employee_id=employee, shift_id="s1",
            authorized_locked=authorize)

    def complete(self, *, order="order1", request="complete1",
                 reservation="lease1", authorize=grant):
        return complete_lifecycle(
            connect, account_id="synthetic", order_id=order,
            action_id=request, actor_id="e1", reservation_id=reservation,
            authorized_locked=authorize)

    def release(self, *, order="order1", request="release1",
                reservation="lease1", reason="Менеджер проверил недостачу",
                authorize=grant):
        return release_lifecycle(
            connect, account_id="synthetic", order_id=order,
            action_id=request, actor_id="manager", reservation_id=reservation,
            reason=reason, authorized_locked=authorize)

    def state(self, order="order1"):
        return read_lifecycle(connect, "synthetic", order)[0]

    def events(self):
        with connect() as c:
            return c.execute("""
                SELECT action,from_state,to_state,reason
                FROM synthetic_line_printer_lifecycle_events ORDER BY event_id
            """).fetchall()

    def test_free_is_default_and_not_persisted(self):
        self.assertEqual(self.state(), "FREE")
        self.assertEqual(self.events(), [])

    def test_first_claim_moves_free_to_reserved(self):
        r = self.reserve()
        self.assertTrue(r.accepted)
        self.assertEqual(self.state(), "RESERVED")
        self.assertEqual(self.events()[0][:3], ("RESERVE", "FREE", "RESERVED"))

    def test_complete_is_terminal_and_audited(self):
        self.reserve()
        self.assertTrue(self.complete().accepted)
        self.assertEqual(self.state(), "COMPLETED")
        self.assertEqual(self.release().reason, "ALREADY_COMPLETED")
        self.assertEqual(self.reserve(request="r2", reservation="lease2").reason,
                         "ALREADY_COMPLETED")
        self.assertEqual(len(self.events()), 2)

    def test_explicit_manager_release_needs_reason_and_authorization(self):
        self.reserve()
        self.assertEqual(self.release(reason="").reason, "RELEASE_REASON_REQUIRED")
        self.assertEqual(self.release(authorize=deny).reason, "AUTHORIZATION_DENIED")
        self.assertEqual(self.state(), "RESERVED")
        self.assertEqual(len(self.events()), 1)
        self.assertTrue(self.release().accepted)
        self.assertEqual(self.state(), "RELEASED")
        self.assertIn("недостачу", self.events()[-1][3])

    def test_released_can_be_reassigned_but_not_old_request(self):
        self.reserve()
        self.release()
        r = self.reserve(request="reserve2", reservation="lease2", printer="p2",
                         employee="e2", actor="e2")
        self.assertTrue(r.accepted)
        self.assertEqual(self.state(), "RESERVED")
        self.assertEqual(self.reserve().reason, "ACTION_ALREADY_RECORDED")
        self.assertEqual(len(self.events()), 3)

    def test_old_reservation_cannot_complete_or_release_new_owner(self):
        self.reserve()
        self.release()
        self.reserve(request="reserve2", reservation="lease2")
        self.assertEqual(self.complete().reason, "RESERVATION_MISMATCH")
        self.assertEqual(self.release(request="release2").reason, "RESERVATION_MISMATCH")
        self.assertEqual(self.state(), "RESERVED")

    def test_unauthorized_claim_does_not_change_state(self):
        self.assertEqual(self.reserve(authorize=deny).reason, "AUTHORIZATION_DENIED")
        self.assertEqual(self.state(), "FREE")
        self.assertEqual(len(self.events()), 0)

    def test_duplicate_action_id_fails_without_replaying_side_effects(self):
        self.reserve()
        self.assertEqual(self.reserve().reason, "ACTION_ALREADY_RECORDED")
        self.assertEqual(self.reserve(request="reserve1", order="other").reason,
                         "ACTION_ID_REUSED")
        self.assertEqual(len(self.events()), 1)

    def test_competing_eight_workers_only_one_claims(self):
        def worker(i):
            return self.reserve(request=f"r{i}", reservation=f"lease{i}",
                                printer=f"p{i}", employee=f"e{i}", actor=f"e{i}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(worker, range(8)))
        self.assertEqual(sum(x.accepted for x in outcomes), 1)
        self.assertEqual(self.state(), "RESERVED")
        self.assertEqual(len(self.events()), 1)

    def test_release_vs_complete_race_only_one_commits(self):
        self.reserve()
        with ThreadPoolExecutor(max_workers=2) as pool:
            a = pool.submit(self.complete)
            b = pool.submit(self.release)
            results = [a.result(), b.result()]
        self.assertEqual(sum(r.accepted for r in results), 1)
        self.assertIn(self.state(), ("COMPLETED", "RELEASED"))
        self.assertEqual(len(self.events()), 2)

    def test_release_refuses_unreserved_order(self):
        self.assertEqual(self.release().reason, "NOT_RESERVED")
        self.assertEqual(self.state(), "FREE")

    def test_failed_authorizer_rolls_back(self):
        def fail(conn, action, previous, record):
            raise RuntimeError("synthetic failure")
        with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
            self.reserve(authorize=fail)
        self.assertEqual(self.state(), "FREE")
        self.assertEqual(self.events(), [])

    def test_order_separation(self):
        self.assertTrue(self.reserve().accepted)
        self.assertTrue(self.reserve(order="order2", request="reserve2",
                                     reservation="lease2").accepted)
        self.assertEqual(len(self.events()), 2)


if __name__ == "__main__":
    unittest.main()
