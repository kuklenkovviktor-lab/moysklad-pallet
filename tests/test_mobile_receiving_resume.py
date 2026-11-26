"""Synthetic regression: persisted receiving counts survive mobile page eviction."""
import os
import tempfile
import unittest
import uuid

_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET","synthetic-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET","synthetic-terminal-secret")
os.environ.setdefault("SECRET_KEY","synthetic-flask-secret")
os.environ.setdefault("MOYSKLAD_TOKEN","synthetic-no-network")
os.environ["WMS_REQUIRE_POSTGRES"]="0"
os.environ.pop("DATABASE_URL",None)
os.environ["WMS_DB_PATH"]=os.path.join(_tmp.name,"tests.db")

import app
from wms import execute
from receiving_idempotency import claim_operation, finish_operation

from attendance_test_support import AttendanceAppFixture


class MobileReceivingResumeTests(AttendanceAppFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_confirmed_only_scoped_to_employee_and_document(self):
        other="other-"+uuid.uuid4().hex
        execute("INSERT INTO employees(id,name,rate,target,active,role) VALUES(?,?,?,?,?,?)",
                (other,"Other receiver",2.25,1600,1,"staff"))
        receipt="receipt-"+uuid.uuid4().hex
        for who,doc,qty in [(self.employee_id,receipt,144),(self.employee_id,receipt,12),
                             (other,receipt,999),(self.employee_id,"different",50)]:
            operation=str(uuid.uuid4())
            claim_operation(operation,doc,who,"PALLET"+operation,"")
            finish_operation(operation,{"added":qty,"total":qty})
        pending=str(uuid.uuid4())
        claim_operation(pending,receipt,self.employee_id,"UNCONFIRMED","")
        response=self.client.get("/api/receiving/session-summary",query_string={"receipt_id":receipt})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(response.json["pallets"],2)
        self.assertEqual(response.json["units"],156.0)
        self.assertEqual(self.client.get("/api/receiving/session-summary").status_code,400)

    def test_restore_is_read_only_and_triggered_on_mobile_return(self):
        self.assertIn("async function refreshReceiptProgress()",app.PAGE)
        self.assertIn("document.addEventListener('visibilitychange'",app.PAGE)
        self.assertIn("window.addEventListener('pageshow'",app.PAGE)
        self.assertTrue(any("receiving_confirmations" in c for c in app.receiving_session_summary.__code__.co_consts if isinstance(c,str)))

if __name__=="__main__":
    unittest.main()
