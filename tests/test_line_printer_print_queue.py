"""Synthetic-only regression tests; no WB API, production DB or physical printer."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor

from line_printer_print_queue import EligibleOrder, PrintQueueError, SyntheticPrintQueue


def candidate(order):
    return EligibleOrder('synthetic', order)


class PrintQueueTests(unittest.TestCase):
    def setUp(self):
        self.flag = patch.dict(os.environ, {'GRIT_SYNTHETIC_PRINT_QUEUE':'YES_ISOLATED'})
        self.flag.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'grit_synthetic_print_queue.sqlite')
        self.q = SyntheticPrintQueue(self.path)

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()
        self.flag.stop()

    def scan(self, printer='A', orders=None):
        return self.q.scan(printer_id=printer, actor='demo-e1',
                           eligible_orders=[candidate(x) for x in (orders or [])],authorized=True)

    def test_flag_required(self):
        with patch.dict(os.environ, {'GRIT_SYNTHETIC_PRINT_QUEUE':''}):
            with self.assertRaises(RuntimeError): SyntheticPrintQueue()

    def test_refuses_non_synthetic_path(self):
        with self.assertRaises(RuntimeError): SyntheticPrintQueue('/tmp/production.sqlite')

    def test_requires_authorization(self):
        with self.assertRaisesRegex(PrintQueueError,'AUTH_REQUIRED'):
            self.q.scan(printer_id='A',actor='worker',eligible_orders=[candidate('1')])

    def test_first_scan_and_duplicate(self):
        self.assertEqual(self.scan(orders=['1','2','2'])['added'],2)
        out = self.scan(orders=['1','2'])
        self.assertEqual(out['added'],0)
        self.assertEqual(out['already_queued'],2)
        self.assertEqual(len(self.q.jobs()),2)

    def test_second_scan_adds_new_only(self):
        self.scan(orders=['1','2'])
        out=self.scan(orders=['1','2','3','4'])
        self.assertEqual(out['added'],2)
        self.assertEqual(len(self.q.jobs()),4)

    def test_other_printer_cannot_take_job(self):
        self.scan(orders=['1'])
        self.assertEqual(self.scan('B',['1'])['owned_elsewhere'],1)
        self.assertEqual(self.q.jobs()[0]['printer_id'],'A')

    def test_reboot_keeps_queue(self):
        self.scan(orders=['1'])
        self.q.close()
        self.q=SyntheticPrintQueue(self.path)
        self.assertEqual(self.scan(orders=['1'])['already_queued'],1)

    def test_no_auto_retry_unknown(self):
        self.scan(orders=['1']); job=self.q.jobs()[0]['id']
        self.q.transition(job_id=job,action_id='send',actor='agent',target='SENDING',authorized=True)
        self.q.transition(job_id=job,action_id='timeout',actor='agent',target='UNKNOWN',authorized=True)
        self.assertEqual(self.scan(orders=['1'])['requires_review'],1)
        self.assertEqual(self.q.jobs()[0]['state'],'UNKNOWN')

    def test_manager_review_retry(self):
        self.test_no_auto_retry_unknown(); job=self.q.jobs()[0]['id']
        with self.assertRaisesRegex(PrintQueueError,'MANAGER_REVIEW_REQUIRED'):
            self.q.transition(job_id=job,action_id='retry',actor='agent',target='QUEUED',authorized=True)
        self.q.transition(job_id=job,action_id='retry',actor='manager',target='QUEUED',authorized=True,
                          reason='paper verified',evidence='REVIEWED_SAFE_TO_RETRY')
        self.assertEqual(self.q.jobs()[0]['state'],'QUEUED')

    def test_spool_ack_not_physical(self):
        self.scan(orders=['1']); job=self.q.jobs()[0]['id']
        self.q.transition(job_id=job,action_id='send',actor='agent',target='SENDING',authorized=True)
        self.q.transition(job_id=job,action_id='ack',actor='agent',target='SUBMITTED',authorized=True)
        with self.assertRaisesRegex(PrintQueueError,'PHYSICAL_CONFIRMATION_REQUIRED'):
            self.q.transition(job_id=job,action_id='printed',actor='agent',target='PRINTED',authorized=True)
        self.assertEqual(self.q.jobs()[0]['state'],'SUBMITTED')

    def test_printed_is_terminal(self):
        self.test_spool_ack_not_physical();job=self.q.jobs()[0]['id']
        self.q.transition(job_id=job,action_id='printed',actor='agent',target='PRINTED',
                          authorized=True,evidence='PHYSICAL_VERIFIED')
        self.assertEqual(self.scan(orders=['1'])['already_handled'],1)
        with self.assertRaises(PrintQueueError):
            self.q.transition(job_id=job,action_id='again',actor='agent',target='SENDING',authorized=True)

    def test_idempotent_transition_and_audit(self):
        self.scan(orders=['1']);job=self.q.jobs()[0]['id']
        for _ in range(2):
            self.q.transition(job_id=job,action_id='send',actor='agent',target='SENDING',authorized=True)
        self.assertEqual(len(self.q.events(job)),2)
        with self.assertRaisesRegex(PrintQueueError,'ACTION_ID_REUSED'):
            self.q.transition(job_id=job,action_id='send',actor='agent',target='FAILED',authorized=True)

    def test_certain_failed_not_auto_retry(self):
        self.scan(orders=['1']);job=self.q.jobs()[0]['id']
        self.q.transition(job_id=job,action_id='send',actor='agent',target='SENDING',authorized=True)
        with self.assertRaisesRegex(PrintQueueError,'CERTAIN_FAILURE_REQUIRED'):
            self.q.transition(job_id=job,action_id='fail',actor='agent',target='FAILED',authorized=True)
        self.q.transition(job_id=job,action_id='fail',actor='agent',target='FAILED',
                          authorized=True,evidence='CERTAIN_NOT_PRINTED')
        self.assertEqual(self.scan(orders=['1'])['requires_review'],1)

    def test_cancel_needs_manager(self):
        self.scan(orders=['1']);job=self.q.jobs()[0]['id']
        with self.assertRaises(PrintQueueError):
            self.q.transition(job_id=job,action_id='x',actor='agent',target='CANCELLED',authorized=True,reason='cancelled')
        self.q.transition(job_id=job,action_id='x',actor='manager',target='CANCELLED',authorized=True,reason='WB cancellation')
        self.assertEqual(self.scan(orders=['1'])['already_handled'],1)

    def test_multiple_threads_no_duplicates(self):
        self.q.close()
        def task(i):
            with SyntheticPrintQueue(self.path) as q:
                return q.scan(printer_id='A' if i%2 else 'B',actor=f'demo-{i}',
                              eligible_orders=[candidate('1')],authorized=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(task,range(8)))
        self.q=SyntheticPrintQueue(self.path)
        self.assertEqual(sum(x['added'] for x in results),1)
        self.assertEqual(len(self.q.jobs()),1)

    def test_different_accounts_same_order_allowed(self):
        self.q.scan(printer_id='A',actor='worker',authorized=True,
                    eligible_orders=[EligibleOrder('synthetic-a','1'),EligibleOrder('synthetic-b','1')])
        self.assertEqual(len(self.q.jobs()),2)

    def test_invalid_batch_is_atomic(self):
        with self.assertRaises(PrintQueueError):
            self.q.scan(printer_id='A',actor='worker',authorized=True,
                        eligible_orders=[candidate('1'),EligibleOrder('synthetic','')])
        self.assertEqual(self.q.jobs(),[])


if __name__ == '__main__': unittest.main()
