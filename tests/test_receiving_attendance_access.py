"""Attendance and authorization gates in the complete app, with synthetic data."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory()
os.environ.update(DATABASE_URL="", WMS_REQUIRE_POSTGRES="0",
                  WMS_DB_PATH=os.path.join(_tmp.name, "synthetic.db"),
                  SECRET_KEY="synthetic", WMS_BADGE_SECRET="synthetic",
                  WMS_TERMINAL_SECRET="synthetic", MOYSKLAD_TOKEN="synthetic-offline-only")

import app
import wms
from attendance_test_support import AttendanceAppFixture
from receiving_idempotency import claim_operation, finish_operation, read_operation


class ReceivingAttendanceAccessTests(AttendanceAppFixture, unittest.TestCase):
    def assert_scan_blocked(self):
        with patch.object(app, "find_product_pack_by_barcode") as lookup, \
             patch.object(app, "add_or_increment_position") as write:
            response = self.client.post("/api/scan", json={
                "operation_id": str(uuid.uuid4()), "receipt_id": "synthetic-receipt",
                "barcode": "synthetic-pallet"})
        self.assertEqual(response.status_code, 401, response.json)
        lookup.assert_not_called()
        write.assert_not_called()

    def test_anonymous_cannot_scan(self):
        self.assert_scan_blocked()

    def test_badge_without_entry_cannot_scan_or_create_visit(self):
        response = self.client.post("/api/auth/login", json={
            "badge_code": wms.make_badge_code(self.employee_id)})
        self.assertEqual(response.status_code, 403, response.json)
        self.assertEqual(wms.query_one("SELECT COUNT(*) FROM attendance_visits")[0], 0)
        self.assertEqual(wms.query_one("SELECT COUNT(*) FROM shifts")[0], 0)
        self.assert_scan_blocked()

    def test_legacy_employee_cookie_without_visit_cannot_scan(self):
        # Negative input only: a cookie from before attendance was mandatory.
        with self.client.session_transaction() as session:
            session["employee_id"] = self.employee_id
        self.assert_scan_blocked()
        with self.client.session_transaction() as session:
            self.assertNotIn("employee_id", session)
        with self.client.session_transaction() as session:
            session["employee_id"] = self.employee_id
        page = self.client.get("/replenishment")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.location.endswith("/login"), page.location)

    def test_foreign_badge_cannot_replace_occupied_terminal(self):
        self.login()
        other = self.create_employee()
        response = self.client.post("/api/auth/login", json={
            "badge_code": wms.make_badge_code(other)})
        self.assertEqual(response.status_code, 409, response.json)
        with self.client.session_transaction() as session:
            self.assertEqual(session["employee_id"], self.employee_id)
        self.assertEqual(wms.query_one(
            "SELECT COUNT(*) FROM attendance_visits WHERE employee_id=?", (other,))[0], 0)

    def test_staff_cannot_mutate_or_print_manager_badges(self):
        self.login()
        response = self.client.post("/api/admin/badges/" + self.employee_id + "/archive")
        self.assertEqual(response.status_code, 403, response.json)
        self.assertEqual(wms.query_one("SELECT active FROM employees WHERE id=?",
                                      (self.employee_id,))[0], 1)
        page = self.client.get("/manager/badges/label")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.location.endswith("/manager-login"), page.location)

    def test_foreign_receiving_operation_cannot_be_read_or_reconciled(self):
        self.login()
        other = self.create_employee()
        operation = str(uuid.uuid4())
        claim_operation(operation, "synthetic-receipt", other, "synthetic-pallet", "")
        finish_operation(operation, {"added": 144, "total": 144})
        self.assertEqual(self.client.get("/api/scan-operation/" + operation).status_code, 404)
        response = self.client.post("/api/scan-operation/" + operation + "/reconcile",
                                    json={"verified": True})
        self.assertEqual(response.status_code, 404, response.json)
        self.assertEqual(read_operation(operation)["status"], "done")


if __name__ == "__main__":
    unittest.main()
