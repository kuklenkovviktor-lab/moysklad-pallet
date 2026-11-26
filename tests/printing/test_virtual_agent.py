"""Disposable PG17 virtual print-agent integration tests (#192).

Uses only synthetic WB fixtures and a PostgreSQL receipt simulator.
No device, real printer, driver, internet, production DB or real WB token.
"""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor

import psycopg

import importlib.util
from pathlib import Path

fixture_spec=importlib.util.spec_from_file_location(
    "pr192_virtual_qr_fixture",
    Path(__file__).resolve().parent / "test_qr_autoqueue.py")
fixture_module=importlib.util.module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixture_module)
from services.printing.virtual_agent import (
    deliver_one_virtual, claim_next_virtual_job, initialize_virtual_sink,
    reconcile_stale_virtual_sending, _accept_at_virtual_sink,
)

if os.getenv("GRIT_SYNTHETIC_PG_TEST") != "YES_ISOLATED":
    raise SystemExit("Isolated PG test gate required")
DSN=os.getenv("PR192_SYNTHETIC_DATABASE_URL","")
if not DSN.startswith("postgresql://postgres:synthetic-only@"):
    raise SystemExit("Unapproved PostgreSQL URL")


def connect():
    return psycopg.connect(DSN)


class VirtualAgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_module.QRQueueHTTPTests.setUpClass()
        with connect() as conn:
            initialize_virtual_sink(conn)

    def setUp(self):
        with connect() as conn:
            conn.execute("DELETE FROM synthetic_pr192_virtual_receipts")
        self.fixture=fixture_module.QRQueueHTTPTests("test_first_scan_queues_without_worker_selecting_order")
        self.fixture.setUp()
        response=self.fixture.scan()
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["queued_new"],1)

    def deliver(self, mode="ACK", printer="p1"):
        return deliver_one_virtual(connect,printer_id=printer,
                                   agent_id="synthetic-agent-1",mode=mode)

    def state(self):
        with connect() as conn:
            return conn.execute("""SELECT state FROM synthetic_pr192_print_jobs
                WHERE order_id='o1'""").fetchone()[0]

    def receipts(self):
        with connect() as conn:
            return conn.execute("""SELECT job_id,printer_id,sticker_digest
               FROM synthetic_pr192_virtual_receipts""").fetchall()

    def events(self):
        with connect() as conn:
            return conn.execute("""SELECT event,from_state,to_state,reason
                FROM synthetic_pr192_print_events ORDER BY event_id""").fetchall()

    def test_ack_records_receipt_but_never_printed(self):
        result=self.deliver()
        self.assertEqual(result.state,"SUBMITTED")
        self.assertEqual(self.state(),"SUBMITTED")
        self.assertEqual(len(self.receipts()),1)
        self.assertEqual(len(self.receipts()[0][2]),64)
        self.assertNotEqual(self.state(),"PRINTED")

    def test_repeat_qr_after_submission_does_not_redeliver(self):
        self.assertEqual(self.deliver().state,"SUBMITTED")
        r=self.fixture.scan()
        self.assertEqual(r.json["queued_new"],0)
        self.assertEqual(r.json["already_queued"],1)
        self.assertEqual(self.deliver().state,"EMPTY")
        self.assertEqual(len(self.receipts()),1)

    def test_concurrent_virtual_agents_claim_once(self):
        def actor(i):
            return deliver_one_virtual(
                connect,printer_id="p1",agent_id=f"synthetic-agent-{i}")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(actor,range(8)))
        self.assertEqual(sum(r.state=="SUBMITTED" for r in results),1)
        self.assertEqual(sum(r.state=="EMPTY" for r in results),7)
        self.assertEqual(len(self.receipts()),1)

    def test_known_failure_stays_failed_without_receipt(self):
        r=self.deliver("CERTAIN_FAILURE")
        self.assertEqual(r.state,"FAILED")
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.fixture.scan().json["requires_review"],1)
        self.assertEqual(self.deliver().state,"EMPTY")

    def test_uncertain_after_virtual_accept_blocks_retry(self):
        r=self.deliver("UNKNOWN_AFTER_ACCEPT")
        self.assertEqual(r.state,"UNKNOWN")
        self.assertEqual(len(self.receipts()),1)
        self.assertEqual(self.fixture.scan().json["requires_review"],1)
        self.assertEqual(self.deliver().state,"EMPTY")
        self.assertEqual(len(self.receipts()),1)

    def test_crash_before_sink_never_retries_automatically(self):
        result=self.deliver("CRASH_BEFORE_SINK")
        self.assertEqual(result.state,"SENDING")
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.fixture.scan().json["already_queued"],1)
        self.assertEqual(self.deliver().state,"EMPTY")
        with connect() as conn:
            conn.execute("""UPDATE synthetic_pr192_print_jobs
               SET updated_at=now()-interval '2 hours'""")
        self.assertEqual(reconcile_stale_virtual_sending(
            connect,agent_id="synthetic-reconciler",older_than_seconds=60),1)
        self.assertEqual(self.state(),"UNKNOWN")
        self.assertEqual(self.deliver().state,"EMPTY")

    def test_crash_after_accept_receipt_persists_but_not_printed(self):
        self.assertEqual(self.deliver("CRASH_AFTER_ACCEPT").state,"SENDING")
        self.assertEqual(len(self.receipts()),1)
        with connect() as conn:
            conn.execute("""UPDATE synthetic_pr192_print_jobs
              SET updated_at=now()-interval '2 hours'""")
        self.assertEqual(reconcile_stale_virtual_sending(
            connect,agent_id="synthetic-reconciler",older_than_seconds=60),1)
        self.assertEqual(self.state(),"UNKNOWN")
        self.assertEqual(self.deliver().state,"EMPTY")
        self.assertEqual(len(self.receipts()),1)

    def test_cancelled_wb_after_queue_is_not_sent(self):
        self.fixture.update("UPDATE wb_fbs_orders SET wb_status='cancel'")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertIn("WB_STATUS_NOT_PICKABLE",r.reason)
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.state(),"CANCELLED")

    def test_completed_order_after_queue_is_not_sent(self):
        self.fixture.update("UPDATE wb_fbs_orders SET local_status='complete'")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertEqual(self.receipts(),[])

    def test_printer_disabled_after_queue_is_not_sent(self):
        self.fixture.update("UPDATE grit_line_printers SET enabled=0")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertIn("PRINTER_DISABLED",r.reason)
        self.assertEqual(self.receipts(),[])

    def test_changed_printer_lines_after_queue_blocks_send(self):
        self.fixture.update("""UPDATE grit_line_printers
          SET lines_json='["В"]' WHERE printer_id='p1'""")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertIn("PRINTER_DOES_NOT_COVER_ORDER",r.reason)
        self.assertEqual(self.receipts(),[])

    def test_missing_sticker_after_queue_blocks_send(self):
        self.fixture.update("DELETE FROM synthetic_pr192_print_stickers")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertIn("ORIGINAL_STICKER_MISSING",r.reason)
        self.assertEqual(self.receipts(),[])

    def test_missing_catalog_after_queue_blocks_send(self):
        self.fixture.update("DELETE FROM fbs_catalog_barcodes")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertIn("CATALOG_BARCODE_MISSING",r.reason)
        self.assertEqual(self.receipts(),[])

    def test_inactive_cell_after_queue_blocks_send(self):
        self.fixture.update("UPDATE replenishment_locations SET active=0")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertEqual(self.receipts(),[])

    def test_removed_from_picking_queue_blocks_send(self):
        self.fixture.update("DELETE FROM wb_fbs_picking_queue")
        r=self.deliver()
        self.assertEqual(r.state,"CANCELLED")
        self.assertEqual(self.receipts(),[])

    def test_other_printer_cannot_claim(self):
        r=self.deliver(printer="other")
        self.assertEqual(r.state,"EMPTY")
        self.assertEqual(self.state(),"QUEUED")

    def test_order_cancelled_between_claim_and_sink_accept(self):
        claim,sticker=claim_next_virtual_job(
            connect,printer_id="p1",agent_id="synthetic-agent-1")
        self.assertEqual(claim.state,"SENDING")
        self.fixture.update("UPDATE wb_fbs_orders SET wb_status='cancel'")
        response=_accept_at_virtual_sink(
            connect,job_id=claim.job_id,printer_id="p1",sticker=sticker)
        self.assertEqual(response.state,"UNKNOWN")
        self.assertIn("WB_STATUS_NOT_PICKABLE",response.reason)
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.state(),"UNKNOWN")
        self.assertEqual(self.deliver().state,"EMPTY")

    def test_sticker_rotated_between_claim_and_sink_accept(self):
        claim,sticker=claim_next_virtual_job(
            connect,printer_id="p1",agent_id="synthetic-agent-1")
        self.assertEqual(claim.state,"SENDING")
        self.fixture.update("""UPDATE synthetic_pr192_print_stickers
          SET original_sticker='SYNTH-REPLACED-LABEL' WHERE order_id='o1'""")
        response=_accept_at_virtual_sink(
            connect,job_id=claim.job_id,printer_id="p1",sticker=sticker)
        self.assertEqual(response.state,"UNKNOWN")
        self.assertIn("ORIGINAL_STICKER_CHANGED",response.reason)
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.state(),"UNKNOWN")

    def test_lines_changed_between_claim_and_sink_accept(self):
        claim,sticker=claim_next_virtual_job(
            connect,printer_id="p1",agent_id="synthetic-agent-1")
        self.assertEqual(claim.state,"SENDING")
        self.fixture.update("""UPDATE grit_line_printers
          SET lines_json='["В"]' WHERE printer_id='p1'""")
        response=_accept_at_virtual_sink(
            connect,job_id=claim.job_id,printer_id="p1",sticker=sticker)
        self.assertEqual(response.state,"UNKNOWN")
        self.assertIn("PRINTER_DOES_NOT_COVER_ORDER",response.reason)
        self.assertEqual(self.receipts(),[])
        self.assertEqual(self.deliver().state,"EMPTY")

    def test_recent_sending_is_not_marked_stale(self):
        self.assertEqual(self.deliver("CRASH_BEFORE_SINK").state,"SENDING")
        self.assertEqual(reconcile_stale_virtual_sending(
            connect,agent_id="synthetic-reconciler",older_than_seconds=300),0)
        self.assertEqual(self.state(),"SENDING")

    def test_submitted_not_changed_by_reconciler(self):
        self.assertEqual(self.deliver().state,"SUBMITTED")
        with connect() as conn:
            conn.execute("""UPDATE synthetic_pr192_print_jobs
                SET updated_at=now()-interval '2 hours'""")
        self.assertEqual(reconcile_stale_virtual_sending(
            connect,agent_id="synthetic-reconciler",older_than_seconds=60),0)
        self.assertEqual(self.state(),"SUBMITTED")

    def test_invalid_mode_does_not_claim(self):
        with self.assertRaises(ValueError):
            self.deliver("UNSAFE_AUTO_REPRINT")
        self.assertEqual(self.state(),"QUEUED")

    def test_event_journal_includes_claim_and_submit(self):
        self.assertEqual(self.deliver().state,"SUBMITTED")
        events=self.events()
        self.assertEqual([x[2] for x in events],["QUEUED","SENDING","SUBMITTED"])
        self.assertEqual(events[-1][1],"SENDING")

    def test_no_automatic_printed_transition_even_with_receipt(self):
        self.deliver()
        with connect() as conn:
            states=conn.execute("""SELECT DISTINCT to_state
                 FROM synthetic_pr192_print_events""").fetchall()
        self.assertNotIn(("PRINTED",),states)


if __name__=="__main__":
    unittest.main()
