"""Synthetic attendance tests. No production DB, badges or warehouse operations."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]

class AttendanceTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{
            "WMS_BADGE_SECRET":"synthetic-badge-secret",
            "WMS_TERMINAL_SECRET":"synthetic-terminal-secret",
            "DATABASE_URL":"",
            "WMS_REQUIRE_POSTGRES":"0",
            "WMS_DB_PATH":str(Path(tmp.name)/"test.db"),
        })
        env.start();self.addCleanup(env.stop)
        spec=importlib.util.spec_from_file_location("wms_attendance_"+uuid.uuid4().hex,ROOT/"wms.py")
        self.wms=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wms)
        sys_mod=patch.dict(sys.modules,{"wms":self.wms})
        sys_mod.start();self.addCleanup(sys_mod.stop)
        spec2=importlib.util.spec_from_file_location("attendance_fixture_"+uuid.uuid4().hex,ROOT/"attendance.py")
        self.attendance=importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(self.attendance)
        self.wms.init_wms_db()
        self.app=Flask(__name__)
        self.app.secret_key="synthetic-session"
        self.app.config.update(TESTING=True)
        self.app.register_blueprint(self.wms.wms_bp)
        self.app.register_blueprint(self.attendance.attendance_bp)
        self.client=self.app.test_client()
        self.attendance.init_attendance_db()
        self.employee_id=self.wms.query_one("SELECT id FROM employees WHERE role!='manager' AND active=1 LIMIT 1")[0]
        self.badge=self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(self.employee_id,))[0]

    def login(self):
        res=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(res.status_code,200,res.json)

    def token(self, path):
        self.assertEqual(self.client.get(path).status_code,200)
        with self.client.session_transaction() as s:
            return s["attendance_csrf"]

    def test_existing_badge_survives_and_attendance_is_independent(self):
        self.login()
        original_shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        token=self.token("/attendance/in")
        first=self.client.post("/attendance/in",data={"csrf":token})
        self.assertEqual(first.status_code,200)
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":token}).status_code,409)
        self.assertEqual(self.client.post("/attendance/out",data={"csrf":token}).status_code,200)
        self.assertEqual(self.client.post("/attendance/out",data={"csrf":token}).status_code,409)
        visit=self.wms.query_one("SELECT arrived_at,left_at FROM attendance_visits WHERE employee_id=?",(self.employee_id,))
        self.assertTrue(visit[0] and visit[1])
        same_shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        self.assertEqual(original_shift,same_shift)
        self.assertEqual(self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(self.employee_id,))[0],self.badge)

    def test_no_post_without_csrf_and_no_guest(self):
        self.assertEqual(self.client.post("/attendance/in",data={}).status_code,302)
        self.login()
        self.assertEqual(self.client.post("/attendance/in",data={}).status_code,403)
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":"wrong"}).status_code,403)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],0)

    def test_manager_only_journal_and_no_manager_in_register(self):
        self.login()
        self.assertEqual(self.client.get("/manager/attendance").status_code,302)
        with self.client.session_transaction() as s:
            s.clear();s["manager_web"]=True
        page=self.client.get("/manager/attendance")
        self.assertEqual(page.status_code,200)
        self.assertIn("Журнал посещаемости".encode(),page.data)
        self.assertNotIn("Виктор".encode(),page.data)
        self.assertEqual(self.client.get("/manager/attendance/qr").status_code,200)

    def test_trainee_profile_and_opt_out(self):
        with self.client.session_transaction() as s:
            s["manager_web"]=True
        self.token("/manager/attendance")
        with self.client.session_transaction() as s:
            token=s["attendance_csrf"]
        res=self.client.post("/manager/attendance/profile",data={
            "csrf":token,"employee_id":self.employee_id,"status":"trainee","day":"2026-11-19",
        })
        self.assertEqual(res.status_code,303)
        row=self.wms.query_one("SELECT employment_status,enabled FROM attendance_profiles WHERE employee_id=?",(self.employee_id,))
        self.assertEqual(row,("trainee",0))
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_audit")[0],1)
        with self.client.session_transaction() as s:
            s.clear()
        self.login()
        self.assertEqual(self.client.get("/attendance/in").status_code,403)

    def test_translucent_pill_navigation(self):
        from grit_theme import GRIT_CSS
        self.assertIn("body .grit-nav-pill", GRIT_CSS)
        self.assertIn("rgba(126,164,199,.12)", GRIT_CSS)
        self.assertIn("backdrop-filter:blur(12px)", GRIT_CSS)
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True
        journal=self.client.get("/manager/attendance")
        self.assertEqual(journal.status_code,200)
        self.assertIn(b'class="grit-nav-pill"', journal.data)
        self.assertIn(b"rgba(126,164,199,.12)",journal.data)
        qr=self.client.get("/manager/attendance/qr")
        self.assertEqual(qr.status_code,200)
        self.assertIn(b'class="grit-nav-pill"',qr.data)

    def test_dark_theme_and_two_qr_links(self):
        with self.client.session_transaction() as s:
            s["manager_web"]=True
        page=self.client.get("/manager/attendance/qr")
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.data.count(b"data:image/png;base64,"),2)
        self.assertIn(b"#09121e",page.data)

if __name__=="__main__":
    unittest.main()
