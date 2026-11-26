"""Integration tests that MUST run only in a disposable PostgreSQL 17 DB."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

from services.printing.pg_queue import (
    enqueue_synthetic_print_pg, initialize_synthetic_print_pg,
    read_synthetic_print_jobs, transition_synthetic_print_pg,
)

if os.getenv('GRIT_SYNTHETIC_PG_TEST') != 'YES_ISOLATED':
    raise SystemExit('Isolated PostgreSQL gate required')
DSN = os.getenv('PR192_SYNTHETIC_DATABASE_URL', '')
if not DSN.startswith('postgresql://postgres:synthetic-only@'):
    raise SystemExit('Refusing non-synthetic DSN')


def connect():
    return psycopg.connect(DSN)


def authorize(conn, printer_id, actor_id):
    return conn.execute('SELECT 1').fetchone()[0] == 1


def eligible(conn, account_id, order_id, kind, printer_id):
    return True


def transition_auth(conn, row, target):
    return True  # Synthetic test callback, NEVER a production authorization.


class PostgresPrintQueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with connect() as conn:
            initialize_synthetic_print_pg(conn)

    def setUp(self):
        with connect() as conn:
            conn.execute('DELETE FROM synthetic_pr192_print_events')
            conn.execute('DELETE FROM synthetic_pr192_print_jobs')

    def scan(self, orders=('1',), printer='P1', account='synthetic',
             eligibility=eligible, auth=authorize):
        return enqueue_synthetic_print_pg(
            connect, printer_id=printer, actor_id='demo-e1',
            candidates=[(account, order, 'WB_FBS') for order in orders],
            authorized_locked=auth, eligible_locked=eligibility)

    def jobs(self, printer='P1'):
        return read_synthetic_print_jobs(connect, printer_id=printer)

    def move(self, target, *, job=None, action='a1', evidence='', reason='',
             actor='demo-agent', auth=transition_auth):
        job = job or self.jobs()[0][0]
        return transition_synthetic_print_pg(
            connect, job_id=job, action_id=action, actor_id=actor,
            target=target, authorized_locked=auth,
            evidence=evidence, reason=reason)

    def test_first_and_repeated_scan(self):
        self.assertEqual(self.scan(('1','2','2')).added, 2)
        repeat = self.scan(('1','2'))
        self.assertEqual((repeat.added, repeat.existing), (0,2))
        self.assertEqual(len(self.jobs()), 2)

    def test_second_scan_with_new_orders(self):
        self.scan(('1','2'))
        result = self.scan(('1','2','3','4'))
        self.assertEqual((result.added,result.existing), (2,2))
        self.assertEqual(len(self.jobs()), 4)

    def test_eight_concurrent_scans_one_owner(self):
        def attempt(i):
            return self.scan(('1',), printer='P'+str(i))
        with ThreadPoolExecutor(max_workers=8) as pool:
            result = list(pool.map(attempt, range(8)))
        self.assertEqual(sum(r.added for r in result), 1)
        self.assertEqual(sum(r.elsewhere for r in result), 7)
        with connect() as conn:
            self.assertEqual(conn.execute(
                'SELECT count(*) FROM synthetic_pr192_print_jobs').fetchone()[0], 1)

    def test_same_order_isolated_by_account(self):
        self.scan(('1',),account='account-A')
        self.scan(('1',),account='account-B')
        self.assertEqual(len(self.jobs()),2)

    def test_failed_authorization_writes_nothing(self):
        result=self.scan(auth=lambda *args: False)
        self.assertEqual(result.reason,'AUTHORIZATION_DENIED')
        self.assertEqual(self.jobs(),[])

    def test_rejected_eligibility_writes_nothing(self):
        result=self.scan(eligibility=lambda *args: False)
        self.assertEqual(result.review,1)
        self.assertEqual(self.jobs(),[])

    def test_callback_exception_rolls_back_entire_batch(self):
        def validator(conn, account, order, kind, printer):
            if order == '2':
                raise RuntimeError('synthetic eligibility exception')
            return True
        with self.assertRaisesRegex(RuntimeError,'synthetic eligibility exception'):
            self.scan(('1','2'), eligibility=validator)
        self.assertEqual(self.jobs(),[])

    def test_missing_callback_fails_closed(self):
        result=self.scan(auth=None)
        self.assertEqual(result.reason,'TRUSTED_VALIDATION_REQUIRED')
        self.assertEqual(self.jobs(),[])

    def test_submitted_is_not_physical_proof(self):
        self.scan()
        self.assertTrue(self.move('SENDING',action='send').accepted)
        self.assertTrue(self.move('SUBMITTED',action='ack').accepted)
        result=self.move('PRINTED',action='physical')
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason,'PHYSICAL_PROOF_REQUIRED')
        self.assertEqual(self.jobs()[0][5], 'SUBMITTED')

    def test_printed_terminal_and_audited(self):
        self.test_submitted_is_not_physical_proof()
        self.assertTrue(self.move('PRINTED',action='verified',evidence='PHYSICAL_VERIFIED').accepted)
        self.assertEqual(self.scan().existing,1)
        self.assertFalse(self.move('SENDING',action='repeat').accepted)
        with connect() as conn:
            actions=conn.execute('SELECT from_state,to_state FROM synthetic_pr192_print_events ORDER BY event_id').fetchall()
        self.assertEqual(actions[-1],('SUBMITTED','PRINTED'))

    def test_unknown_requires_manager_review(self):
        self.scan()
        self.move('SENDING',action='send')
        self.move('UNKNOWN',action='lost')
        self.assertEqual(self.scan().review,1)
        self.assertEqual(self.move('QUEUED',action='unsafe').reason,'MANAGER_REVIEW_REQUIRED')
        self.assertTrue(self.move('QUEUED',action='safe',actor='demo-manager',
                                  evidence='REVIEWED_SAFE_TO_RETRY',reason='Checked device status').accepted)

    def test_failed_requires_certainty(self):
        self.scan();self.move('SENDING',action='send')
        self.assertEqual(self.move('FAILED',action='bad').reason,'FAILURE_PROOF_REQUIRED')
        self.assertTrue(self.move('FAILED',action='certain',evidence='CERTAIN_NOT_PRINTED').accepted)
        self.assertEqual(self.scan().review,1)

    def test_repeated_action_has_no_second_effect(self):
        self.scan(); self.assertTrue(self.move('SENDING',action='once').accepted)
        self.assertEqual(self.move('SENDING',action='once').reason,'ACTION_ALREADY_RECORDED')
        with connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM synthetic_pr192_print_events WHERE action_id='once'").fetchone()[0],1)

    def test_action_id_cannot_apply_to_other_job(self):
        self.scan(('1','2'));a,b=self.jobs()
        self.assertTrue(self.move('SENDING',job=a[0],action='once').accepted)
        self.assertEqual(self.move('SENDING',job=b[0],action='once').reason,'ACTION_ID_REUSED')

    def test_cancel_requires_reason(self):
        self.scan()
        self.assertEqual(self.move('CANCELLED',action='bad').reason,'CANCELLATION_REASON_REQUIRED')
        self.assertTrue(self.move('CANCELLED',action='cancel',reason='WB order cancelled').accepted)
        self.assertFalse(self.move('QUEUED',action='uncancel').accepted)

    def test_refuses_disabled_gate(self):
        previous=os.environ.pop('GRIT_SYNTHETIC_PG_TEST',None)
        try:
            with self.assertRaisesRegex(RuntimeError, 'gate'):
                self.scan()
        finally:
            if previous is not None: os.environ['GRIT_SYNTHETIC_PG_TEST']=previous


if __name__ == '__main__':
    unittest.main()
