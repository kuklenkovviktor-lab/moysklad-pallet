"""Replenishment creation and two-stage verification, without stock movements."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "replenishment.db")

import app
from wms import execute, init_wms_db, query_one


class ReplenishmentFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app._WMS_READY.set()
        cls.product = {"id": "product-id-1", "article": "SKU_TEST",
                       "name": "Test product"}

    def setUp(self):
        self.client = app.app.test_client()
        self.location = "T" + uuid.uuid4().hex[:10].upper()
        self.location_code = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
        execute(
            "INSERT INTO replenishment_locations (id, label, barcode, created_at) VALUES (?,?,?,?)",
            (str(uuid.uuid4()), self.location, self.location_code, "2026-10-22T00:00:00Z"),
        )
        self.as_employee("anton")

    def as_employee(self, name):
        with self.client.session_transaction() as s:
            s.clear()
            s["employee_id"] = name

    def self_create(self, code="4601234567890", line=None):
        with patch.object(app, "find_product_by_any_barcode",
                          return_value=(self.product, None)) as lookup:
            result = self.client.post("/api/replenishment/self-create",
                json={"barcode": code, "line": line or self.location})
        return result, lookup

    def test_picker_creates_task_and_verifies_location_before_completion(self):
        created, lookup = self.self_create()
        self.assertEqual(created.status_code, 201, created.json)
        lookup.assert_called_once_with("4601234567890")
        task = created.json["task_id"]

        row = query_one(
            "SELECT status, assigned_employee_id, article, verify_required, "
            "verified_product_barcode, verified_product_at, verified_location_at "
            "FROM replenishment_tasks WHERE id = ?", (task,),
        )
        self.assertEqual(row[0:5], ("started", "anton", "SKU_TEST", 1, "4601234567890"))
        self.assertIsNotNone(row[5])
        self.assertIsNone(row[6])

        listing = self.client.get("/api/replenishment/tasks")
        self.assertEqual(listing.status_code, 200, listing.json)
        self.assertTrue(any(x["label"] == self.location for x in listing.json["locations"]))
        self.assertTrue(any(x["id"] == task for x in listing.json["tasks"]))

        early = self.client.post("/api/replenishment/action",
             json={"task_id": task, "action": "complete", "quantity": 12})
        self.assertEqual(early.status_code, 409, early.json)

        wrong = self.client.post("/api/replenishment/verify",
             json={"task_id": task, "stage": "location", "barcode": "WRONG"})
        self.assertIn(wrong.status_code, (404, 409))

        verified = self.client.post("/api/replenishment/verify",
             json={"task_id": task, "stage": "location", "barcode": self.location_code})
        self.assertEqual(verified.status_code, 200, verified.json)

        self.as_employee("ilya")
        denied = self.client.post("/api/replenishment/action",
             json={"task_id": task, "action": "complete", "quantity": 12})
        self.assertEqual(denied.status_code, 409, denied.json)
        self.as_employee("anton")

        completed = self.client.post("/api/replenishment/action",
             json={"task_id": task, "action": "complete", "quantity": 12})
        self.assertEqual(completed.status_code, 200, completed.json)
        record = query_one("SELECT status, completed_qty FROM replenishment_tasks WHERE id=?", (task,))
        self.assertEqual(record, ("completed", 12))
        self.assertEqual(
            self.client.post("/api/replenishment/action",
                json={"task_id": task, "action": "complete", "quantity": 12}).status_code, 409
        )

    def test_duplicate_active_task_is_blocked_without_duplicate_events(self):
        first, _ = self.self_create()
        self.assertEqual(first.status_code, 201, first.json)
        second, _ = self.self_create()
        self.assertEqual(second.status_code, 409, second.json)
        self.assertIn("уже есть", second.json["error"])
        self.assertEqual(query_one(
            "SELECT COUNT(*) FROM replenishment_tasks WHERE article=? AND line_code=? "
            "AND status IN ('new','started')", ("SKU_TEST", self.location))[0], 1)

    def test_unknown_location_and_unknown_product_do_not_create_task(self):
        result, _ = self.self_create(line="NO_SUCH_CELL")
        self.assertEqual(result.status_code, 409, result.json)
        with patch.object(app, "find_product_by_any_barcode", return_value=(None, None)):
            missing = self.client.post("/api/replenishment/self-create",
                json={"barcode": "9999999", "line": self.location})
        self.assertEqual(missing.status_code, 404, missing.json)

    def test_worker_self_create_requires_badge(self):
        with self.client.session_transaction() as s:
            s.clear()
        answer = self.client.post("/api/replenishment/self-create",
            json={"barcode": "4601234567890", "line": self.location})
        self.assertEqual(answer.status_code, 401)

    def test_manager_can_create_task_with_registered_product_and_location(self):
        with self.client.session_transaction() as s:
            s.clear()
            s["manager_web"] = True
        with patch.object(app, "_get_product_list", return_value=[self.product]):
            created = self.client.post("/api/manager/replenishment/create",
              json={"product_id": self.product["id"], "article": self.product["article"],
                    "line": self.location, "planned_qty": 18, "priority": "normal", "note": ""})
        self.assertEqual(created.status_code, 201, created.json)
        self.assertEqual(query_one(
            "SELECT status, planned_qty, verify_required FROM replenishment_tasks WHERE id=?",
            (created.json["task_id"],)), ("new", 18, 1))

    def test_worker_page_has_self_create_and_safe_json_handling(self):
        from replenishment import MANAGER_PAGE, WORKER_PAGE
        self.assertIn("/api/replenishment/self-create", WORKER_PAGE)
        self.assertIn("selfLocation", WORKER_PAGE)
        self.assertIn("readApiJson(r)", WORKER_PAGE)
        self.assertIn("readApiJson(r)", MANAGER_PAGE)


if __name__ == "__main__":
    unittest.main()
