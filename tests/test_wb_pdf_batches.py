"""SQLite integration smoke tests for manager-only WB batch intake."""
import io
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from flask import Flask

TEMP = tempfile.TemporaryDirectory()
DB = os.path.join(TEMP.name, "test.db")

wms_stub = types.ModuleType("wms")
wms_stub.DATABASE_URL = ""
@contextmanager
def db_connect():
    conn = sqlite3.connect(DB)
    try:
        yield conn
    finally:
        conn.close()
wms_stub.db_connect = db_connect
wms_stub.manager_access = lambda: True
wms_stub.sql = lambda q: q
_previous_wms = sys.modules.get("wms")
sys.modules["wms"] = wms_stub

labels_stub = types.ModuleType("fbs_pdf_labels")
labels_stub.parse_wb_labels = lambda doc: None
labels_stub.attach_active_locations = lambda report: report
_previous_labels = sys.modules.get("fbs_pdf_labels")
sys.modules["fbs_pdf_labels"] = labels_stub
import wb_pdf_batches as batches
import wb_print_queue as queue
import onix_wb_queue_agent as agent
# Restore process-wide module lookup after loading the isolated test subjects.
if _previous_wms is None:
    sys.modules.pop("wms", None)
else:
    sys.modules["wms"] = _previous_wms
if _previous_labels is None:
    sys.modules.pop("fbs_pdf_labels", None)
else:
    sys.modules["fbs_pdf_labels"] = _previous_labels


def make_report():
    return {
        "locations_available": True,
        "valid_structure": True,
        "unique_articles": 3,
        "unique_sticker_barcodes": 4,
        "products": [
            {"article":"1349_NONE","product_barcode":"2042741355108",
             "locations":["E-12"],"stickers":[{"barcode":"59393228243","page":2},{"barcode":"59393228244","page":3}]},
            {"article":"UNKNOWN","product_barcode":"1234567890123",
             "locations":[],"stickers":[{"barcode":"59393228245","page":5}]},
            {"article":"MULTI","product_barcode":"1234567890124",
             "locations":["A-01","B-01"],"stickers":[{"barcode":"59393228246","page":7}]},
        ],
    }


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask(__name__)
        app.register_blueprint(batches.wb_batches_bp)
        app.register_blueprint(queue.wb_print_queue_bp)
        cls.client = app.test_client()

    def upload(self, raw=b"%PDF-VALID", headers=None):
        return self.client.post("/api/manager/wb-batches/import",
            data={"file": (io.BytesIO(raw), "wb.pdf")},
            content_type="multipart/form-data",
            headers=headers or {"Origin":"https://onix.example"},
            base_url="https://onix.example")

    def test_import_and_idempotency(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            first = self.upload()
            self.assertEqual(first.status_code, 201, first.get_json())
            data = first.get_json()
            self.assertEqual(data["sticker_count"], 4)
            self.assertEqual(data["print_jobs_created"], 0)
            self.assertEqual(data["routing"], {"located":2,"unassigned":1,"ambiguous":1})
            repeat = self.upload()
            self.assertEqual(repeat.status_code, 200, repeat.get_json())
            self.assertTrue(repeat.get_json()["duplicate"])
            self.assertEqual(repeat.get_json()["batch_id"], data["batch_id"])
        with db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_pdf_batches WHERE id=?",(data["batch_id"],)).fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_pdf_batch_stickers WHERE batch_id=?",(data["batch_id"],)).fetchone()[0], 4)

    def test_read_only_route_preview(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            data = self.upload(raw=b"%PDF-ROUTE").get_json()
        batch_id = data["batch_id"]
        r = self.client.get(f"/api/manager/wb-batches/{batch_id}/routes?lines=E,A")
        self.assertEqual(r.status_code, 200, r.get_json())
        out = r.get_json()
        self.assertEqual(out["counts"], {"A": 0, "E": 2})
        self.assertEqual(out["blocked"], {"blocked_unassigned": 1, "blocked_ambiguous": 1})
        self.assertEqual(out["print_jobs_created"], 0)
        self.assertEqual(len(out["selected_orders"]), 2)
        self.assertEqual(self.client.get(f"/api/manager/wb-batches/{batch_id}/routes?lines=A,INVALID").status_code, 400)

    def test_queue_idempotency_and_test_printer_only(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            batch_id=self.upload(raw=b"%PDF-QUEUE").get_json()["batch_id"]
        route=f"/api/manager/wb-batches/{batch_id}/print-queue"
        data={"confirmation":"reserve-onix-test-only","lines":["E"],"printer":"ONIX-TEST-XP460B"}
        kwargs={"json":data,"base_url":"https://onix.example","headers":{"Origin":"https://onix.example"}}
        first=self.client.post(route,**kwargs)
        self.assertEqual(first.status_code,200,first.get_json())
        self.assertEqual(first.get_json()["created"],2)
        second=self.client.post(route,**kwargs)
        self.assertEqual(second.status_code,200)
        self.assertEqual(second.get_json()["created"],0)
        self.assertEqual(second.get_json()["already_reserved"],2)
        bad=dict(data,printer="Xprinter XP-460B")
        self.assertEqual(self.client.post(route,json=bad,base_url="https://onix.example",
            headers={"Origin":"https://onix.example"}).status_code,400)
        with db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_route_print_jobs WHERE batch_id=?",(batch_id,)).fetchone()[0],2)

    def test_agent_preview_read_only_and_authenticated(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            batch_id = self.upload(raw=b"%PDF-AGENT").get_json()["batch_id"]
        route=f"/api/manager/wb-batches/{batch_id}/print-queue"
        self.client.post(route,json={"confirmation":"reserve-onix-test-only","lines":["E"],
                                   "printer":"ONIX-TEST-XP460B"},
            base_url="https://onix.example",headers={"Origin":"https://onix.example"})
        with patch.dict(os.environ, {"ONIX_PRINT_AGENT_TOKEN":"s"*40}):
            endpoint="/api/print-agent/wb/preview-next"
            self.assertEqual(self.client.get(endpoint).status_code,401)
            good=self.client.get(endpoint,headers={"Authorization":"Bearer "+"s"*40})
            self.assertEqual(good.status_code,200,good.get_json())
            self.assertEqual(good.get_json()["wb_code"],"59393228243")
            self.assertEqual(good.get_json()["mode"],"dry_run_only")
            with db_connect() as conn:
                status=conn.execute("SELECT status FROM wb_route_print_jobs WHERE id=?",
                                    (good.get_json()["job_id"],)).fetchone()[0]
                self.assertEqual(status,"queued")

    def test_atomic_claim_and_ack_no_requeue(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            batch_id=self.upload(raw=b"%PDF-CLAIM").get_json()["batch_id"]
        route=f"/api/manager/wb-batches/{batch_id}/print-queue"
        self.client.post(route,json={"confirmation":"reserve-onix-test-only",
                 "lines":["E"],"printer":"ONIX-TEST-XP460B"},
                 headers={"Origin":"https://onix.example"},base_url="https://onix.example")
        auth={"Authorization":"Bearer "+"s"*40}
        with patch.dict(os.environ,{"ONIX_PRINT_AGENT_TOKEN":"s"*40}):
            one=self.client.post("/api/print-agent/wb/claim-one",headers=auth)
            self.assertEqual(one.status_code,200,one.get_json())
            job=one.get_json()
            self.assertEqual(job["mode"],"claim_no_auto_retry")
            two=self.client.post("/api/print-agent/wb/claim-one",headers=auth)
            self.assertEqual(two.status_code,200,two.get_json())
            self.assertNotEqual(job["job_id"],two.get_json()["job_id"])
            three=self.client.post("/api/print-agent/wb/claim-one",headers=auth)
            self.assertEqual(three.status_code,200)
            self.assertNotIn(three.get_json()["job_id"], (job["job_id"],two.get_json()["job_id"]))
            ack=self.client.post("/api/print-agent/wb/ack",headers=auth,
                                 json={"job_id":job["job_id"],"status":"uncertain"})
            self.assertEqual(ack.status_code,200,ack.get_json())
            self.assertFalse(ack.get_json()["physical_print_confirmed"])
            duplicate=self.client.post("/api/print-agent/wb/ack",headers=auth,
                                       json={"job_id":job["job_id"],"status":"submitted"})
            self.assertEqual(duplicate.status_code,409)
            self.assertEqual(self.client.post("/api/print-agent/wb/claim-one").status_code,401)

    def test_agent_spools_once_on_valid_claim(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            pdf=Path(directory)/"wb.pdf"
            pdf.write_bytes(b"%PDF-FAKE-CONTENT")
            digest=__import__("hashlib").sha256(pdf.read_bytes()).hexdigest()
            base={"mode":"dry_run_only","printer":"ONIX-TEST-XP460B",
                  "pdf_sha256":digest,"wb_code":"59393228243",
                  "article":"1349_NONE","page_no":2,"job_id":"test-job",
                  "batch_id":"batch-one"}
            claimed=dict(base,mode="claim_no_auto_retry")
            replies=[base,claimed,{"ok":True}]
            def remote(*args,**kwargs):return replies.pop(0)
            metadata={"wb_code":"59393228243","article":"1349_NONE",
                      "page":2,"target_mm":[58,40]}
            with patch.object(agent,"remote",side_effect=remote), \
                 patch.object(agent,"validate_job",return_value={"verified":True}), \
                 patch.object(agent,"read_and_select",return_value=(metadata,b"bitmap")), \
                 patch.object(agent,"_spool_one") as printer:
                agent.do_print_one("s"*40,str(pdf),object())
                printer.assert_called_once()

    def test_agent_refuses_changed_job_without_spooling(self):
        with tempfile.TemporaryDirectory() as directory:
            from pathlib import Path
            pdf=Path(directory)/"wb.pdf"
            pdf.write_bytes(b"%PDF-FAKE")
            first={"mode":"dry_run_only","printer":"ONIX-TEST-XP460B",
                   "job_id":"first","wb_code":"59393228243","article":"1349_NONE",
                   "page_no":2,"batch_id":"b","pdf_sha256":"x"}
            changed=dict(first,mode="claim_no_auto_retry",job_id="different")
            replies=[first,changed,{"ok":True}]
            with patch.object(agent,"remote",side_effect=lambda *a,**k:replies.pop(0)), \
                 patch.object(agent,"validate_job",return_value={"verified":True}), \
                 patch.object(agent,"_spool_one") as printer:
                with self.assertRaises(ValueError):
                    agent.do_print_one("s"*40,str(pdf),object())
                printer.assert_not_called()

    def test_invalid_origin_and_pdf(self):
        self.assertEqual(self.upload(headers={"Origin":"https://evil.example"}).status_code, 403)
        self.assertEqual(self.upload(raw=b"NOT A PDF").status_code, 400)

    def test_invalid_structure_rejected(self):
        with patch.object(batches, "_parse", side_effect=ValueError("invalid")):
            self.assertEqual(self.upload(raw=b"%PDF-OTHER").status_code, 422)


if __name__ == "__main__":
    unittest.main()
