"""Isolated app fixtures: real QR entry, badge login, attendance and shift binding."""
import socket
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import app
import attendance
import wms
from receiving_idempotency import init_receiving_operations


class AttendanceAppFixture:
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory(prefix="grit-regression-")
        self.addCleanup(tmp.cleanup)
        for guard in (
            patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")),
            patch.object(wms, "DATABASE_URL", ""),
            patch.object(wms, "DB_PATH", str(Path(tmp.name) / "synthetic.db")),
            patch.object(wms, "_DB_READY", False),
            patch.object(app, "_start_wms_initializer"),
        ):
            guard.start()
            self.addCleanup(guard.stop)
        wms.init_wms_db()
        init_receiving_operations()
        attendance.init_attendance_db()
        app._WMS_READY.set()
        self.client = app.app.test_client()
        self.employee_id = self.create_employee()

    def create_employee(self):
        employee_id = "synthetic-" + uuid.uuid4().hex
        wms.execute(
            "INSERT INTO employees(id,name,rate,target,active,role,badge_code) VALUES(?,?,?,?,?,?,?)",
            (employee_id, "Synthetic receiver", 2.25, 1600, 1, "staff",
             wms.make_badge_code(employee_id)),
        )
        return employee_id

    def login(self, client=None, employee_id=None):
        client = client or self.client
        employee_id = employee_id or self.employee_id
        intent = client.post("/api/attendance/entry-intent",
                             json={"qr": "https://localhost/attendance/in"},
                             base_url="https://localhost")
        self.assertEqual(intent.status_code, 200, intent.json)
        badge = wms.query_one("SELECT badge_code FROM employees WHERE id=?", (employee_id,))[0]
        response = client.post("/api/auth/login", json={"badge_code": badge})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(wms.query_one(
            "SELECT COUNT(*) FROM attendance_visits WHERE employee_id=? AND left_at IS NULL",
            (employee_id,))[0], 1)
        with client.session_transaction() as session:
            self.assertEqual(session["employee_id"], employee_id)
            self.assertEqual(session["employee_shift_id"], response.json["shift_id"])
            self.assertTrue(session["employee_shift_id"])
