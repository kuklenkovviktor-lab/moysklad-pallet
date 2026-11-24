"""Synthetic attendance tests. No production DB, badges or warehouse operations."""
import importlib.util
import os
from pathlib import Path
import sys
import time
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
        attendance_modules=patch.dict(sys.modules,{"attendance":self.attendance})
        attendance_modules.start();self.addCleanup(attendance_modules.stop)
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
        intent=self.client.post("/api/attendance/entry-intent",
            json={"qr":"https://localhost/attendance/in"},base_url="https://localhost")
        self.assertEqual(intent.status_code,200,intent.json)
        res=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(res.status_code,200,res.json)

    def token(self, path):
        self.assertEqual(self.client.get(path).status_code,200)
        with self.client.session_transaction() as s:
            return s["attendance_csrf"]

    def test_badge_only_and_old_short_code_cannot_enter(self):
        for code in (self.badge, "3", "ONIX-BADGE-3"):
            with self.subTest(code=code):
                res=self.client.post("/api/auth/login",json={"badge_code":code})
                self.assertNotEqual(res.status_code,200)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],0)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM shifts")[0],0)

    def test_login_url_next_does_not_authorize_badge(self):
        self.assertEqual(self.client.get("/login?next=/attendance/in").status_code,200)
        res=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(res.status_code,403)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM shifts")[0],0)

    def test_expired_qr_intent_rejects_badge_without_journal(self):
        with self.client.session_transaction() as sess:
            sess["attendance_entry_intent_at"]=time.time()-121
        res=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(res.status_code,403)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],0)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM shifts")[0],0)

    def test_journal_failure_cannot_grant_terminal_session(self):
        self.assertEqual(self.client.post("/api/attendance/entry-intent",
            json={"qr":"https://localhost/attendance/in"},base_url="https://localhost").status_code,200)
        import attendance
        with patch.object(attendance,"record_badge_entry",side_effect=RuntimeError("synthetic DB failure")):
            res=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(res.status_code,409)
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM shifts")[0],0)

    def test_entry_intent_rejects_non_qr_strings(self):
        for qr in ("WMS-BADGE-FAKE","https://localhost/attendance/out",
                   "https://evil.example/attendance/in",
                   "https://localhost/attendance/in?next=/fbs"):
            self.assertEqual(self.client.post("/api/attendance/entry-intent",
                json={"qr":qr},base_url="https://localhost").status_code,403)
        self.assertEqual(self.client.post("/api/auth/login",
            json={"badge_code":self.badge}).status_code,403)

    def test_valid_qr_badge_immediately_records_arrival(self):
        self.login()
        row=self.wms.query_one("SELECT arrived_at,source,left_at FROM attendance_visits WHERE employee_id=?",
                               (self.employee_id,))
        self.assertTrue(row[0])
        self.assertEqual(row[1],"qr")
        self.assertIsNone(row[2])
        self.assertEqual(self.client.get("/api/auth/me").status_code,200)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess["attendance_verified"])
            self.assertNotIn("attendance_entry_intent_at",sess)

    def test_entry_badge_exit_closes_shift_and_clears_session(self):
        self.login()
        original_shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        arrival_page=self.client.get("/attendance/in")
        self.assertEqual(arrival_page.status_code,200)
        self.assertIn(b'requestSubmit()',arrival_page.data)
        self.assertIn(b'id="attendance-action"',arrival_page.data)
        self.assertNotIn("Подтвердить вход".encode(),arrival_page.data)
        self.assertNotIn("Отмечаем время на сервере".encode(),arrival_page.data)
        self.assertIn(b'style="display:none"',arrival_page.data)
        with self.client.session_transaction() as sess:
            csrf=sess["attendance_csrf"]
        entry=self.client.post("/attendance/in",data={"csrf":csrf})
        self.assertEqual(entry.status_code,303)
        self.assertEqual(entry.headers["Location"],"/")
        # Rescanning entry must neither duplicate a visit nor strand the user.
        duplicate_entry=self.client.post("/attendance/in",data={"csrf":csrf})
        self.assertEqual(duplicate_entry.status_code,303)
        self.assertEqual(duplicate_entry.headers["Location"],"/")
        self.assertEqual(self.wms.query_one(
            "SELECT COUNT(*) FROM attendance_visits WHERE employee_id=?",
            (self.employee_id,))[0],1)
        departure_page=self.client.get("/attendance/out")
        self.assertNotIn(b'requestSubmit()',departure_page.data)
        self.assertIn('Отсканируй QR ВЫХОД'.encode(), departure_page.data)
        exit_response=self.client.post("/attendance/out",data={"csrf":csrf,"exit_qr":self.attendance.exit_qr_value()})
        self.assertEqual(exit_response.status_code,303)
        self.assertEqual(exit_response.headers["Location"],"/login?attendance_out=1")
        self.assertEqual(self.client.get("/api/auth/me").status_code,401)
        visit=self.wms.query_one("SELECT arrived_at,left_at FROM attendance_visits WHERE employee_id=?",(self.employee_id,))
        self.assertTrue(visit[0] and visit[1])
        self.assertIsNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(original_shift,)))
        self.assertIsNotNone(self.wms.query_one("SELECT ended_at FROM shifts WHERE id=?",(original_shift,))[0])
        self.assertIsNone(self.wms.query_one("SELECT id FROM activities WHERE shift_id=? AND ended_at IS NULL",(original_shift,)))
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM operation_events WHERE shift_id=? AND operation_type='shift' AND event_type='end'",(original_shift,)))
        with self.client.session_transaction() as sess:
            self.assertNotIn("employee_id",sess)
        self.assertEqual(self.client.get("/attendance/out").status_code,302)
        login_page=self.client.get("/login?attendance_out=1")
        self.assertEqual(login_page.status_code,200)
        self.assertIn(b"attendance_out",login_page.data)
        self.assertIn(b"localStorage.removeItem(k)",login_page.data)
        self.assertIn(b"history.replaceState(null,'','/login')",login_page.data)
        self.assertEqual(self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(self.employee_id,))[0],self.badge)

    def test_failed_shift_close_rolls_back_departure(self):
        self.login()
        shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        csrf=self.token("/attendance/in")
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":csrf}).status_code,303)
        self.client.get("/attendance/exit-scan")
        with patch("fbs_transactions.event",side_effect=RuntimeError("synthetic failure")):
            response=self.client.post("/attendance/out",data={"csrf":csrf,"exit_qr":self.attendance.exit_qr_value()})
        self.assertEqual(response.status_code,409)
        self.assertIn(b'href="/attendance/out"',response.data)
        self.assertIn(b'href="/"',response.data)
        self.assertIsNone(self.wms.query_one("SELECT left_at FROM attendance_visits WHERE employee_id=?",(self.employee_id,))[0])
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(shift,)))
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"],self.employee_id)

    def test_departure_without_arrival_does_not_close_shift(self):
        self.login()
        shift=self.wms.query_one("SELECT id FROM shifts WHERE employee_id=? AND ended_at IS NULL",(self.employee_id,))[0]
        self.wms.execute("DELETE FROM attendance_visits WHERE employee_id=?", (self.employee_id,))
        csrf=self.token("/attendance/out")
        response=self.client.post("/attendance/out",data={"csrf":csrf,"exit_qr":self.attendance.exit_qr_value()})
        self.assertEqual(response.status_code,403)
        self.assertIsNotNone(self.wms.query_one("SELECT id FROM shifts WHERE id=? AND ended_at IS NULL",(shift,)))
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],0)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"],self.employee_id)

    def test_supervisor_qr_is_no_longer_required_or_offered(self):
        self.login()
        self.assertEqual(self.client.get("/exit").headers["Location"],"/attendance/out")
        self.assertEqual(self.client.post("/api/auth/logout",json={}).status_code,410)
        self.assertEqual(self.client.get("/api/admin/supervisor-exit-code").status_code,410)
        self.assertEqual(self.client.post("/api/supervisor-terminal/activate",json={}).status_code,410)
        self.assertEqual(self.client.get("/api/supervisor-terminal/qr").status_code,410)
        self.assertEqual(self.client.get("/supervisor-terminal").headers["Location"],"/manager/attendance/qr")
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["manager_web"]=True
        self.assertEqual(self.client.get("/supervisor-qr").headers["Location"],"/manager")
        from ast import parse,Assign,Constant
        wms_ast=parse((ROOT/"wms.py").read_text(encoding="utf-8"))
        html=next(node.value.value for node in wms_ast.body
                  if isinstance(node,Assign) and any(getattr(t,"id",None)=="FBS_PAGE" for t in node.targets)
                  and isinstance(node.value,Constant))
        self.assertIn('href="/attendance/exit-scan"',html)
        self.assertNotIn('href="/exit"',html)
        manager_html=next(node.value.value for node in wms_ast.body
                          if isinstance(node,Assign) and any(getattr(t,"id",None)=="STATS_PAGE" for t in node.targets)
                          and isinstance(node.value,Constant))
        self.assertIn('href="/manager/attendance/qr"',manager_html)
        self.assertNotIn('openExitQr()',manager_html)
        self.assertNotIn('exitQrModal',manager_html)

    def test_no_post_without_csrf_and_no_guest(self):
        self.assertEqual(self.client.post("/attendance/in",data={}).status_code,302)
        self.login()
        self.assertEqual(self.client.post("/attendance/in",data={}).status_code,403)
        self.assertEqual(self.client.post("/attendance/in",data={"csrf":"wrong"}).status_code,403)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0],1)

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
        self.assertEqual(self.client.post("/api/attendance/entry-intent",
            json={"qr":"https://localhost/attendance/in"},
            base_url="https://localhost").status_code,200)
        blocked=self.client.post("/api/auth/login",json={"badge_code":self.badge})
        self.assertEqual(blocked.status_code,409)
        self.assertEqual(self.client.get("/attendance/in").status_code,302)

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
        self.assertIn("if(!setAttendanceAction(attendanceActionFromQr(code),code)){",login_html)
        self.assertIn("// Keep the camera open to scan the personal badge next.",login_html)
        self.assertIn("if(setAttendanceAction(attendanceActionFromQr(code),code))return;",login_html)
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
        self.assertIn(b"location.replace(nextTarget)",page.data)
        self.assertIn(b"catch(storageError)",page.data)
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


    def test_exit_scan_request_is_bounded_and_keeps_journal_authoritative(self):
        self.login()
        page = self.client.get("/attendance/out")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"new AbortController()", page.data)
        self.assertIn(b"setTimeout(()=>controller.abort(),8000)", page.data)
        self.assertIn(b"signal:controller.signal", page.data)
        self.assertIn(b"clearTimeout(timeout)", page.data)
        self.assertIn("Проверь журнал перед повторным сканированием".encode(), page.data)
        # A view or timeout never writes to the attendance journal.
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0], 1)

    def test_exit_get_and_unproven_posts_preserve_terminal(self):
        self.login()
        csrf = self.token("/attendance/in")
        self.client.post("/attendance/in", data={"csrf": csrf})
        before = self.wms.query_one("SELECT left_at FROM attendance_visits")
        for path in ("/attendance/out", "/attendance/exit-scan"):
            result = self.client.get(path)
            self.assertEqual(result.status_code, 200)
            self.assertNotIn(self.attendance.exit_qr_value().encode(), result.data)
        for code in ("", "https://gritwms.space/attendance/out", "WRONG"):
            response = self.client.post("/attendance/out", data={"csrf":csrf, "exit_qr":code})
            self.assertEqual(response.status_code,403)
        self.assertEqual(self.wms.query_one("SELECT left_at FROM attendance_visits"), before)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["employee_id"], self.employee_id)

    def test_double_exit_has_one_end_event(self):
        self.login()
        csrf = self.token("/attendance/in")
        self.client.post("/attendance/in", data={"csrf":csrf})
        self.client.get("/attendance/exit-scan")
        payload = {"csrf":csrf, "exit_qr":self.attendance.exit_qr_value()}
        self.assertEqual(self.client.post("/attendance/out", data=payload).status_code,303)
        self.client.post("/attendance/out", data=payload)
        self.assertEqual(self.wms.query_one(
            "SELECT COUNT(*) FROM operation_events WHERE operation_type='shift' AND event_type='end'")[0],1)

if __name__=="__main__":
    unittest.main()
