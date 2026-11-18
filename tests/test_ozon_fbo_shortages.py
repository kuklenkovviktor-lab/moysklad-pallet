"""Ozon FBO shortage workflow: synthetic SQLite, no live Ozon, MoySklad or HOSTKEY."""
import os
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET","synthetic-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET","synthetic-terminal")
os.environ.setdefault("SECRET_KEY","synthetic-session")
os.environ["WMS_REQUIRE_POSTGRES"]="0"
os.environ.pop("DATABASE_URL",None)
_tmp=tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"]=os.path.join(_tmp.name,"synthetic.sqlite")

from flask import Flask
from wms import execute, query_one
from ozon_fbo_picking import fbo_picking_bp, _ensure_picking_db

class FboShortageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=Flask(__name__)
        cls.app.secret_key="synthetic"
        cls.app.testing=True
        cls.app.register_blueprint(fbo_picking_bp)
        _ensure_picking_db()

    def setUp(self):
        self.client=self.app.test_client()
        self.supply=str(uuid.uuid4())
        self.cargo=str(uuid.uuid4())
        self.item=str(uuid.uuid4())
        execute("""INSERT INTO ozon_fbo_supplies
          (id,title,source_filename,source_hash,plan_hash,created_at,created_by,places,qty,lines_count,warning_count)
          VALUES (?,?,?,?,?,?,?,?,?,?,?)""",(self.supply,"Synthetic","test.xlsx",
          str(uuid.uuid4()),str(uuid.uuid4()),"2026-11-14T00:00:00Z","fixture",1,5,1,0))
        execute("""INSERT INTO ozon_fbo_cargo
          (id,supply_id,barcode,destination,label,planned_qty,lines_count)
          VALUES (?,?,?,?,?,?,?)""",(self.cargo,self.supply,"GM123456789","Test","Fixture",5,1))
        execute("""INSERT INTO ozon_fbo_items
          (id,cargo_id,barcode,article,planned_qty,source_cell)
          VALUES (?,?,?,?,?,?)""",(self.item,self.cargo,"SKU123","ART-1",5,"Test!A2"))
        self.employee("anton")
        self.assertEqual(self.cargo_post("claim",{}).status_code,200)

    def tearDown(self):
        execute("UPDATE ozon_fbo_cargo SET claimed_employee_id=NULL WHERE id=?", (self.cargo,))

    def employee(self,who):
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["employee_id"]=who

    def manager(self):
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["manager_web"]=True

    def cargo_post(self,action,payload):
        return self.client.post("/api/ozon-fbo/pick/cargo/"+self.cargo+"/"+action,json=payload)

    def scan(self,qty):
        return self.cargo_post("scan",{"barcode":"SKU123","quantity":qty,
                                     "request_id":str(uuid.uuid4())})

    def report(self,key=None,reason="Фактический остаток меньше плана",item=None):
        return self.cargo_post("shortage",{"item_id":item or self.item,"reason":reason,
                                          "request_id":key or str(uuid.uuid4())})

    def decide(self,decision,reason="Проверено руководителем"):
        return self.client.post("/api/manager/ozon-fbo/shortage/decide",json={
            "cargo_id":self.cargo,"item_id":self.item,
            "decision":decision,"reason":reason})

    def test_city_article_shortage_register_uses_approved_only(self):
        self.assertEqual(self.scan(3).status_code, 200)
        self.assertEqual(self.report().status_code, 200)
        shortage_id = query_one("SELECT id FROM ozon_fbo_shortages WHERE item_id=?", (self.item,))[0]
        self.manager()
        before = self.client.get("/api/manager/ozon-fbo/shortages/register")
        self.assertEqual(before.status_code, 200)
        self.assertFalse(any(x["id"] == shortage_id for x in before.json["rows"]))
        self.assertEqual(self.decide("approve").status_code, 200)
        after = self.client.get("/api/manager/ozon-fbo/shortages/register")
        self.assertEqual(after.status_code, 200)
        rows = [x for x in after.json["rows"] if x["id"] == shortage_id]
        self.assertTrue(rows)
        self.assertEqual(rows[0]["city"], "Test")
        self.assertEqual(rows[0]["article"], "ART-1")
        self.assertEqual(rows[0]["missing"], 2)
        self.assertTrue(any(x["city"] == "Test" and x["article"] == "ART-1" and x["missing"] >= 2 for x in after.json["summary"]))

    def test_approve_shortage_then_existing_qr_can_close(self):
        self.assertEqual(self.scan(3).status_code,200)
        reported=self.report()
        self.assertEqual(reported.status_code,200,reported.json)
        self.assertEqual(reported.json["cargo"]["picked"],3)
        self.assertEqual(reported.json["cargo"]["remaining"],2)
        self.assertEqual(reported.json["cargo"]["pending_shortages"],1)
        self.assertFalse(reported.json["cargo"]["ready_for_qr"])
        self.assertEqual(self.cargo_post("complete",{"cargo_barcode":"GM123456789"}).status_code,409)
        self.assertEqual(self.scan(1).status_code,409)
        self.manager()
        approved=self.decide("approve")
        self.assertEqual(approved.status_code,200,approved.json)
        self.assertEqual(approved.json["cargo"]["approved_shortage"],2)
        self.assertTrue(approved.json["cargo"]["ready_for_qr"])
        self.employee("anton")
        self.assertEqual(self.cargo_post("complete",{"cargo_barcode":"wrong"}).status_code,422)
        done=self.cargo_post("complete",{"cargo_barcode":"GM123456789"})
        self.assertEqual(done.status_code,200,done.json)
        self.assertEqual(done.json["cargo"]["status"],"done")
        self.assertEqual(done.json["cargo"]["qty"],5)
        self.assertEqual(done.json["cargo"]["picked"],3)
        self.assertEqual(done.json["cargo"]["approved_shortage"],2)
        self.assertEqual(query_one("SELECT COUNT(*) FROM ozon_fbo_pick_events WHERE cargo_id=?",(self.cargo,))[0],1)

    def test_duplicate_report_is_idempotent_and_wrong_payload_denied(self):
        key=str(uuid.uuid4())
        first=self.report(key=key)
        self.assertEqual(first.status_code,200,first.json)
        repeat=self.report(key=key)
        self.assertEqual(repeat.status_code,200,repeat.json)
        self.assertTrue(repeat.json["replayed"])
        self.assertEqual(self.report(key=key,reason="Другая причина для подмены").status_code,409)
        self.assertEqual(self.report().status_code,409)
        self.assertEqual(query_one("SELECT COUNT(*) FROM ozon_fbo_shortages WHERE cargo_id=?",(self.cargo,))[0],1)
        self.assertEqual(query_one("SELECT COUNT(*) FROM ozon_fbo_pick_audit WHERE cargo_id=? AND action='shortage_report'",(self.cargo,))[0],1)

    def test_manager_rejection_reopens_picking_and_can_report_again(self):
        self.assertEqual(self.report().status_code,200)
        self.manager()
        self.assertEqual(self.decide("reject").status_code,200)
        self.assertEqual(self.decide("approve").status_code,409)
        self.employee("anton")
        self.assertEqual(self.scan(2).status_code,200)
        next_report=self.report()
        self.assertEqual(next_report.status_code,200,next_report.json)
        self.assertEqual(next_report.json["cargo"]["remaining"],3)
        self.assertEqual(next_report.json["cargo"]["pending_shortages"],1)

    def test_multiple_missing_skus_require_individual_manager_approval(self):
        second=str(uuid.uuid4())
        execute("INSERT INTO ozon_fbo_items (id,cargo_id,barcode,article,planned_qty,source_cell) VALUES (?,?,?,?,?,?)",
                (second,self.cargo,"SKU456","ART-2",4,"Test!E2"))
        execute("UPDATE ozon_fbo_cargo SET planned_qty=9,lines_count=2 WHERE id=?",(self.cargo,))
        first=self.report()
        self.assertEqual(first.status_code,200,first.json)
        second_report=self.report(item=second)
        self.assertEqual(second_report.status_code,200,second_report.json)
        self.assertEqual(second_report.json["cargo"]["pending_shortages"],2)
        self.manager()
        self.assertEqual(self.decide("approve").status_code,200)
        self.employee("anton")
        state=self.client.get("/api/ozon-fbo/pick/cargo/"+self.cargo).json["cargo"]
        self.assertFalse(state["ready_for_qr"])
        self.manager()
        approved=self.client.post("/api/manager/ozon-fbo/shortage/decide",json={
            "cargo_id":self.cargo,"item_id":second,"decision":"approve",
            "reason":"Проверено по учёту и ячейке"})
        self.assertEqual(approved.status_code,200,approved.json)
        self.assertEqual(approved.json["cargo"]["approved_shortage"],9)
        self.assertTrue(approved.json["cargo"]["ready_for_qr"])

    def test_manager_only_and_cross_employee_blocked(self):
        self.assertEqual(self.decide("approve").status_code,403)
        self.assertEqual(self.report(item="other").status_code,404)
        self.employee("ilya")
        self.assertEqual(self.report().status_code,403)
        self.employee("anton")
        self.assertEqual(self.report().status_code,200)

    def test_undo_cannot_change_item_with_pending_shortage(self):
        self.assertEqual(self.scan(2).status_code,200)
        self.assertEqual(self.report().status_code,200)
        undo=self.cargo_post("undo",{"request_id":str(uuid.uuid4())})
        self.assertEqual(undo.status_code,409,undo.json)
        self.assertEqual(query_one("SELECT picked_qty FROM ozon_fbo_items WHERE id=?",(self.item,))[0],2)

if __name__=="__main__":
    unittest.main()
