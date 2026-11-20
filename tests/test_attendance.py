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

    def test_entry_badge_exit_closes_shift_and_clears_session(self):
        self.login()
        original_shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        arrival_page=self.client.get("/attendance/in")
        self.assertEqual(arrival_page.status_code,200)
        self.assertIn(b'requestSubmit()',arrival_page.data)
        self.assertIn(b'id="attendance-action"',arrival_page.data)
        self.assertNotIn("Подтвердить вход".encode(),arrival_page.data)
        with self.client.session_transaction() as sess:
            csrf=sess["attendance_csrf"]
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":csrf}).status_code,200)
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":csrf}).status_code,409)
        departure_page=self.client.get("/attendance/out")
        self.assertIn(b'requestSubmit()',departure_page.data)
        self.assertEqual(self.client.post("/attendance/out",data={"csrf":csrf}).status_code,200)
        visit=self.wms.query_one("SELECT arrived_at,left_at FROM attendance_visits WHERE employee_id=?",(self.employee_id,))
        self.assertTrue(visit[0] and visit[1])
        self.assertIsNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(original_shift,)))
        self.assertIsNotNone(self.wms.query_one("SELECT ended_at FROM shifts WHERE id=?",(original_shift,))[0])
        self.assertIsNone(self.wms.query_one("SELECT id FROM activities WHERE shift_id=? AND ended_at IS NULL",(original_shift,)))
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM operation_events WHERE shift_id=? AND operation_type='shift' AND event_type='end'",(original_shift,)))
        with self.client.session_transaction() as sess:
            self.assertNotIn("employee_id",sess)
        self.assertEqual(self.client.get("/attendance/out").status_code,302)
        self.assertEqual(self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(self.employee_id,))[0],self.badge)

    def test_failed_shift_close_rolls_back_departure(self):
        self.login()
        shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        csrf=self.token("/attendance/in")
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":csrf}).status_code,200)
        with patch("fbs_transactions.event",side_effect=RuntimeError("synthetic failure")):
            response=self.client.post("/attendance/out",data={"csrf":csrf})
        self.assertEqual(response.status_code,409)
        self.assertIsNone(self.wms.query_one("SELECT left_at FROM attendance_visits WHERE employee_id=?",(self.employee_id,))[0])
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(shift,)))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"],self.employee_id)

    def test_departure_without_arrival_does_not_close_shift(self):
        self.login()
        shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        csrf=self.token("/attendance/out")
        response=self.client.post("/attendance/out",data={"csrf":csrf})
        self.assertEqual(response.status_code,409)
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(shift,)))
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],0)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"],self.employee_id)

    def test_supervisor_qr_is_no_longer_required_or_offered(self):
        self.login()
        self.assertEqual(self.client.get("/exit").headers["Location"],"/attendance/out")
        self.assertEqual(self.client.post("/api/auth/logout",json={}).status_code,410)
        self.assertEqual(self.client.get("/api/admin/supervisor-exit-code").status_code,410)
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["manager_web"]=True
        self.assertEqual(self.client.get("/supervisor-qr").headers["Location"],"/manager")
        from ast import parse,Assign,Constant
        wms_ast=parse((ROOT/"wms.py").read_text(encoding="utf-8"))
        html=next(node.value.value for node in wms_ast.body
                  if isinstance(node,Assign) and any(getattr(t,"id",None)=="FBS_PAGE" for t in node.targets)
                  and isinstance(node.value,Constant))
        self.assertIn('href="/attendance/out"',html)
        self.assertNotIn('href="/exit"',html)

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

    def test_camera_and_hid_scans_route_action_qr_before_badge(self):
        # The login camera used to POST the attendance URL as a badge and fail.
        # Preserve camera scanning for the second (employee badge) QR.
        from ast import parse, Assign, Constant
        module=parse((ROOT/"wms.py").read_text(encoding="utf-8"))
        login_html=next(
            node.value.value for node in module.body
            if isinstance(node,Assign)
            and any(getattr(t,"id",None)=="LOGIN_PAGE" for t in node.targets)
            and isinstance(node.value,Constant)
        )
        self.assertIn("function attendanceActionFromQr(value)",login_html)
        self.assertIn("parsed.origin!==location.origin",login_html)
        self.assertIn("parsed.pathname==='/attendance/in'",login_html)
        self.assertIn("parsed.pathname==='/attendance/out'",login_html)
        self.assertIn("pendingAttendanceAction=action",login_html)
        self.assertIn("pendingAttendanceAction ||",login_html)
        self.assertIn("if(!setAttendanceAction(attendanceActionFromQr(code))){",login_html)
        self.assertIn("// Keep the camera open to scan the personal badge next.",login_html)
        self.assertIn("if(setAttendanceAction(attendanceActionFromQr(code)))return;",login_html)
        self.assertIn("await login(code)",login_html)

    def test_qr_to_badge_login_returns_to_correct_action(self):
        # Both QR entry points preserve the intended action without an employee session.
        for action in ("in", "out"):
            with self.subTest(action=action):
                response=self.client.get("/attendance/"+action)
                self.assertEqual(response.status_code,302)
                self.assertEqual(response.headers["Location"],"/login?next=/attendance/"+action)

        # Badge login is not altered for ordinary FBS logins.
        page=self.client.get("/login?next=/attendance/out")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"requestedNext",page.data)
        self.assertIn(b"nextTarget",page.data)
        self.assertIn(b"'/attendance/in'",page.data)
        self.assertIn(b"'/attendance/out'",page.data)

        self.login()
        self.assertEqual(self.client.get("/login?next=/attendance/in").headers["Location"],"/attendance/in")
        self.assertEqual(self.client.get("/login?next=/attendance/out").headers["Location"],"/attendance/out")
        # External URLs cannot be used as post-login redirects.
        external=self.client.get("/login?next=https://evil.example")
        self.assertEqual(external.status_code,302)
        self.assertNotIn("evil.example",external.headers["Location"])

    def test_production_auth_gate_preserves_attendance_intent(self):
        source=(ROOT/"app.py").read_text(encoding="utf-8")
        self.assertIn('if path in ("/attendance/in", "/attendance/out"):',source)
        self.assertIn('return redirect("/login?next=" + path)',source)

    def test_qr_print_two_exact_58x40mm_labels(self):
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True
        response = self.client.get("/manager/attendance/qr")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"@page{size:58mm 40mm;margin:0}", response.data)
        self.assertIn(b"width:58mm!important;height:40mm!important", response.data)
        self.assertIn(b"page-break-after:always", response.data)
        self.assertIn(b"section.attendance-label:last-child", response.data)
        self.assertEqual(response.data.count(b'class="panel attendance-label"'), 2)
        self.assertEqual(response.data.count(b"data:image/png;base64,"), 2)

    def test_dark_theme_and_two_qr_links(self):
        with self.client.session_transaction() as s:
            s["manager_web"]=True
        page=self.client.get("/manager/attendance/qr")
        self.assertEqual(page.status_code,200)
        self.assertEqual(page.data.count(b"data:image/png;base64,"),2)
        self.assertIn(b"#09121e",page.data)

if __name__=="__main__":
    unittest.main()
