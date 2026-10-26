"""Integration tests for isolated Ozon FBO cargo picking, no live MoySklad calls."""
import os
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET", "test-only-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-only-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-only-flask-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)

_TMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TMP.name, "test-wms.sqlite")

from flask import Flask
from wms import execute
from ozon_fbo_picking import fbo_picking_bp, _ensure_picking_db


class FboPickingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Flask(__name__)
        cls.app.secret_key = "test-only"
        cls.app.register_blueprint(fbo_picking_bp)
        cls.app.testing = True
        _ensure_picking_db()

    def setUp(self):
        self.client = self.app.test_client()
        self.sid = str(uuid.uuid4())
        self.cid = str(uuid.uuid4())
        self.item = str(uuid.uuid4())
        execute("""INSERT INTO ozon_fbo_supplies (
             id,title,source_filename,source_hash,plan_hash,created_at,created_by,
             places,qty,lines_count,warning_count)
             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
             (self.sid,"FBO Test","test.xlsx",str(uuid.uuid4()),str(uuid.uuid4()),
              "2026-10-16T00:00:00Z","Test",1,5,1,0))
        execute("""INSERT INTO ozon_fbo_cargo
             (id,supply_id,barcode,destination,label,planned_qty,lines_count)
             VALUES (?,?,?,?,?,?,?)""",
             (self.cid,self.sid,"1000000000063374464","Москва","мск8",5,1))
        execute("""INSERT INTO ozon_fbo_items
             (id,cargo_id,barcode,article,planned_qty,source_cell)
             VALUES (?,?,?,?,?,?)""",
             (self.item,self.cid,"4601234567890","TEST-ART",5,"Москва!A2"))
        self.as_employee("anton")

    def tearDown(self):
        # Isolate the one-active-cargo-per-employee invariant across tests.
        # Every test creates its own cargo; release its employee claim.
        execute("UPDATE ozon_fbo_cargo SET claimed_employee_id=NULL WHERE id=?", (self.cid,))

    def as_employee(self, value):
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["employee_id"] = value

    def post(self, action, data):
        return self.client.post("/api/ozon-fbo/pick/cargo/"+self.cid+"/"+action,json=data)

    def scan(self, qty, barcode="4601234567890", request_id=None):
        return self.post("scan",{"barcode":barcode,"quantity":qty,
                                 "request_id":request_id or str(uuid.uuid4())})

    def test_claim_and_no_other_user_can_scan(self):
        r=self.post("claim",{})
        self.assertEqual(r.status_code,200,r.json)
        self.as_employee("ilya")
        self.assertEqual(self.post("claim",{}).status_code,409)
        self.assertEqual(self.scan(1).status_code,409)
        self.as_employee("anton")
        self.assertEqual(self.scan(1).status_code,200)

    def test_scan_guards_idempotency_and_finish(self):
        self.assertEqual(self.post("claim",{}).status_code,200)
        key=str(uuid.uuid4())
        first=self.scan(3,request_id=key)
        self.assertEqual(first.status_code,200,first.json)
        self.assertEqual(first.json["cargo"]["picked"],3)
        replay=self.scan(3,request_id=key)
        self.assertEqual(replay.status_code,200)
        self.assertTrue(replay.json["replayed"])
        self.assertEqual(replay.json["cargo"]["picked"],3)
        self.assertEqual(self.scan(1,barcode="wrong").status_code,422)
        self.assertEqual(self.scan(3).status_code,409)
        self.assertEqual(self.post("complete",{"cargo_barcode":"1000000000063374464"}).status_code,409)
        self.assertEqual(self.scan(2).json["cargo"]["remaining"],0)
        self.assertEqual(self.post("complete",{"cargo_barcode":"wrong"}).status_code,422)
        finished=self.post("complete",{"cargo_barcode":"1000000000063374464"})
        self.assertEqual(finished.status_code,200,finished.json)
        self.assertEqual(finished.json["cargo"]["status"],"done")
        repeated=self.post("complete",{"cargo_barcode":"1000000000063374464"})
        self.assertEqual(repeated.status_code,200)
        self.assertTrue(repeated.json["already_done"])
        self.assertEqual(self.scan(1).status_code,409)

    def test_undo_replays_once_and_handover_keeps_counts(self):
        self.assertEqual(self.post("claim",{}).status_code,200)
        self.assertEqual(self.scan(2).status_code,200)
        key=str(uuid.uuid4())
        undo=self.post("undo",{"request_id":key})
        self.assertEqual(undo.status_code,200,undo.json)
        self.assertEqual(undo.json["cargo"]["picked"],0)
        repeated=self.post("undo",{"request_id":key})
        self.assertEqual(repeated.status_code,200,repeated.json)
        self.assertEqual(repeated.json["cargo"]["picked"],0)
        self.assertTrue(repeated.json["replayed"])
        self.assertEqual(self.scan(1).status_code,200)
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["manager_web"]=True
        released=self.client.post("/api/manager/ozon-fbo/release",
                                  json={"cargo_id":self.cid,"reason":"Передача задания Илье"})
        self.assertEqual(released.status_code,200,released.json)
        self.as_employee("ilya")
        claimed=self.post("claim",{})
        self.assertEqual(claimed.status_code,200,claimed.json)
        self.assertEqual(claimed.json["cargo"]["picked"],1)
        self.assertEqual(self.scan(4).status_code,200)

    def test_state_is_server_side_after_new_browser(self):
        self.post("claim",{})
        self.scan(2)
        self.client = self.app.test_client()
        self.as_employee("anton")
        active=self.client.get("/api/ozon-fbo/pick/active")
        self.assertEqual(active.status_code,200)
        self.assertEqual(active.json["cargo_id"],self.cid)
        detail=self.client.get("/api/ozon-fbo/pick/cargo/"+self.cid)
        self.assertEqual(detail.json["cargo"]["picked"],2)


if __name__ == "__main__":
    unittest.main()
