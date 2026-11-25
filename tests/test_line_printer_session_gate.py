"""Pure synthetic session identity checks. No Flask/production imports."""
import unittest
from line_printer_session_gate import require_bound_terminal_identity


class TerminalIdentityTests(unittest.TestCase):
    def setUp(self):
        self.browser = {
            "employee_id": "e1",
            "employee_shift_id": "shift-e1",
            "attendance_verified": True,
        }
        self.employee = {"id": "e1", "name": "Синтетический сотрудник"}

    def test_valid_bound_session(self):
        self.assertEqual(
            require_bound_terminal_identity(self.browser, self.employee),
            ("e1", "shift-e1"))

    def test_missing_auth_employee_fails_closed(self):
        self.assertIsNone(require_bound_terminal_identity(self.browser, None))

    def test_cookie_employee_mismatch_denied(self):
        self.browser["employee_id"] = "e2"
        self.assertIsNone(require_bound_terminal_identity(self.browser, self.employee))

    def test_no_bound_shift_denied(self):
        self.browser.pop("employee_shift_id")
        self.assertIsNone(require_bound_terminal_identity(self.browser, self.employee))

    def test_attendance_must_be_verified(self):
        self.browser["attendance_verified"] = False
        self.assertIsNone(require_bound_terminal_identity(self.browser, self.employee))

    def test_manager_cookie_cannot_claim_as_worker(self):
        self.browser["manager_web"] = True
        self.assertIsNone(require_bound_terminal_identity(self.browser, self.employee))

    def test_request_body_identity_cannot_override_session(self):
        malicious_body = {"employee_id": "admin", "shift_id": "admin-shift"}
        self.assertEqual(malicious_body["employee_id"], "admin")
        self.assertEqual(
            require_bound_terminal_identity(self.browser, self.employee),
            ("e1", "shift-e1"))


if __name__ == "__main__":
    unittest.main()
