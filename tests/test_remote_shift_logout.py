"""Remote shift-close logout tests. All records synthetic, no live servers."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

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

class RemoteShiftLogoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app._WMS_READY.set()

    def setUp(self):
        p=patch.object(app,"_start_wms_initializer")
        p.start()
        self.addCleanup(p.stop)
        self.employee="receiver-"+uuid.uuid4().hex
        self.shift_id=str(uuid.uuid4())
        self.client=app.app.test_client()
        execute("INSERT INTO employees(id,name,rate,target,active,role) VALUES(?,?,?,?,?,?)",
                (self.employee,"Synthetic receiver",2.25,1600,1,"staff"))
        execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,NULL)",
                (self.shift_id,self.employee,(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat()))
        with self.client.session_transaction() as ses:
            ses.clear()
            ses["employee_id"]=self.employee
            ses["employee_shift_id"]=self.shift_id
            ses.permanent=True

    def manager(self):
        manager=app.app.test_client()
        with manager.session_transaction() as ses:
            ses.clear()
            ses["manager_web"]=True
            ses["manager_login_at"]=datetime.now(timezone.utc).timestamp()
        return manager

    def test_manager_closure_revokes_auth_and_receiving_api(self):
        self.assertEqual(self.client.get("/api/auth/me").status_code,200)
        ended=(datetime.now(timezone.utc)-timedelta(seconds=15)).isoformat()
        response=self.manager().post("/api/manager/shifts/force-close",json={
            "shift_id":self.shift_id,"reason":"Synthetic shift closed by supervisor",
            "actual_ended_at":ended})
        self.assertEqual(response.status_code,200,response.json)
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)
        self.assertEqual(self.client.get("/api/shift/current").status_code,401)
        self.assertEqual(self.client.get("/api/receiving/session-summary",query_string={"receipt_id":"synthetic"}).status_code,401)

    def test_stale_cookie_does_not_attach_new_shift(self):
        execute("UPDATE shifts SET ended_at=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(),self.shift_id))
        next_id=str(uuid.uuid4())
        execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,NULL)",
                (next_id,self.employee,datetime.now(timezone.utc).isoformat()))
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)

    def test_terminal_pages_receive_client_poll_not_manager(self):
        self.assertIn("setInterval(verifyShift,15000)",app._TERMINAL_RECHECK_JS)
        self.assertIn("window.addEventListener('pageshow',verifyShift)",app._TERMINAL_RECHECK_JS)
        self.assertIn("DO NOT remove the receiving pending operation UUID",app._TERMINAL_RECHECK_JS)
        page=self.client.get("/")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"verifyShift()",page.data)
        manager=self.client.get("/manager")
        if manager.status_code==200:
            self.assertNotIn(b"setInterval(verifyShift,15000)",manager.data)

if __name__=="__main__":
    unittest.main()
