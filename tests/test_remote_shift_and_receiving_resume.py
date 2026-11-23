"""Regressions for remote manager shift closure and mobile receiving resume.

These tests operate on an isolated SQLite database with synthetic records;
they never call MoySklad or connect to HOSTKEY.
"""
import os
import tempfile
import unittest
import uuid
import json
from datetime import datetime, timedelta, timezone

_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET","synthetic-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET","synthetic-terminal-secret")
os.environ.setdefault("SECRET_KEY","synthetic-flask-secret")
os.environ.setdefault("MOYSKLAD_TOKEN","synthetic-no-network")
os.environ["WMS_REQUIRE_POSTGRES"]="0"
os.environ.pop("DATABASE_URL",None)
os.environ["WMS_DB_PATH"]=os.path.join(_tmp.name,"tests.db")

import app
from wms import init_wms_db, execute
from receiving_idempotency import init_receiving_operations, claim_operation, finish_operation


class RemoteShiftAndReceivingResumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        init_receiving_operations()
        app._WMS_READY.set()

    def setUp(self):
        from unittest.mock import patch
        self.mock_start=patch.object(app,"_start_wms_initializer")
        self.mock_start.start()
        self.addCleanup(self.mock_start.stop)
        self.client=app.app.test_client()
        self.employee_id="synthetic-"+uuid.uuid4().hex
        self.shift_id=str(uuid.uuid4())
        self.start=datetime.now(timezone.utc)-timedelta(minutes=20)
        execute("INSERT INTO employees(id,name,rate,target,active,role) VALUES(?,?,?,?,?,?)",
                (self.employee_id,"Synthetic receiver",2.25,1600,1,"staff"))
        execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,NULL)",
                (self.shift_id,self.employee_id,self.start.isoformat()))
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["employee_id"]=self.employee_id
            ses["employee_shift_id"]=self.shift_id
            ses.permanent=True

    def _manager(self):
        manager=app.app.test_client()
        with manager.session_transaction() as ses:
            ses.clear()
            ses["manager_web"]=True
            ses["manager_login_at"]=datetime.now(timezone.utc).timestamp()
        return manager

    def test_manager_force_close_revokes_existing_employee_cookie(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code,200)
        manager=self._manager()
        actual=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        res=manager.post("/api/manager/shifts/force-close",json={
            "shift_id":self.shift_id,
            "reason":"Synthetic test: shift ended by supervisor",
            "actual_ended_at":actual,
        })
        self.assertEqual(res.status_code,200,res.json)
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)
        self.assertEqual(self.client.get("/api/shift/current").status_code,401)

    def test_old_cookie_cannot_authorize_new_shift(self):
        from wms import get_session_employee
        with app.app.test_request_context("/api/auth/me"):
            from flask import session
            session["employee_id"]=self.employee_id
            session["employee_shift_id"]=self.shift_id
            session.permanent=True
            execute("UPDATE shifts SET ended_at=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(),self.shift_id))
            next_id=str(uuid.uuid4())
            execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,NULL)",
                    (next_id,self.employee_id,datetime.now(timezone.utc).isoformat()))
            self.assertIsNone(get_session_employee())

    def test_summary_is_durable_scoped_and_excludes_uncertain(self):
        other_id="other-"+uuid.uuid4().hex
        execute("INSERT INTO employees(id,name,rate,target,active,role) VALUES(?,?,?,?,?,?)",
                (other_id,"Other",2.25,1600,1,"staff"))
        doc="receipt-"+uuid.uuid4().hex
        for who,qty in [(self.employee_id,144),(self.employee_id,12),(other_id,999)]:
            op=str(uuid.uuid4())
            claim_operation(op,doc,who,"PALLET"+op,"")
            finish_operation(op,{"added":qty,"total":qty})
        unresolved=str(uuid.uuid4())
        claim_operation(unresolved,doc,self.employee_id,"PENDING","")
        got=self.client.get("/api/receiving/session-summary",query_string={"receipt_id":doc})
        self.assertEqual(got.status_code,200,got.json)
        self.assertEqual(got.json["pallets"],2)
        self.assertEqual(got.json["units"],156.0)
        self.assertEqual(self.client.get("/api/receiving/session-summary").status_code,400)

    def test_terminal_refresh_never_replays_receiving_write(self):
        self.assertIn("setInterval(verifyShift,15000)",app._TERMINAL_RECHECK_JS)
        self.assertIn("refreshReceiptProgress()",app.PAGE)
        self.assertIn("receiving/session-summary",app.PAGE)
        self.assertIn("DO NOT remove the receiving pending operation UUID",
                      app._TERMINAL_RECHECK_JS)


if __name__=="__main__":
    unittest.main()
