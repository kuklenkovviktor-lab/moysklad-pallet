"""Safe ONIX pallet receiving: one confirmation UUID, at most one MoySklad write."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

_temp = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET", "test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal-secret")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only-placeholder")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_temp.name, "onix-test.db")

import app
from receiving_idempotency import init_receiving_operations, read_operation
from wms import init_wms_db


class ReceivingConfirmationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        init_receiving_operations()
        app._WMS_READY.set()

    def setUp(self):
        self.start = patch.object(app, "_start_wms_initializer")
        self.start.start()
        self.addCleanup(self.start.stop)
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["employee_id"] = "viktor"
        self.product = {"id": "product", "article": "SKU", "name": "Item",
                        "meta": {"href": "https://example.test/product", "type": "product"}}
        self.pack = {"name": "Палета", "quantity": 144}

    def payload(self, op=None, qty=None):
        return {"operation_id": op or str(uuid.uuid4()),
                "receipt_id": "receipt-1", "barcode": "PALLET144", "quantity": qty}

    def test_single_confirm_then_retry_is_idempotent(self):
        p = self.payload()
        with patch.object(app, "find_product_pack_by_barcode", return_value=(self.product, self.pack)) as lookup, \
             patch.object(app, "add_or_increment_position", return_value={"mode": "created", "added": 144, "total": 144}) as add, \
             patch.object(app, "log_event") as audit:
            first = self.client.post("/api/scan", json=p)
            again = self.client.post("/api/scan", json=p)
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(again.status_code, 200, again.json)
        self.assertTrue(again.json["replayed"])
        self.assertEqual(first.json["added"], 144)
        add.assert_called_once()
        lookup.assert_called_once()
        audit.assert_called_once()
        self.assertEqual(read_operation(p["operation_id"])["status"], "done")

    def test_next_physical_pallet_same_barcode_new_uuid(self):
        with patch.object(app, "find_product_pack_by_barcode", return_value=(self.product, self.pack)), \
             patch.object(app, "add_or_increment_position", return_value={"mode": "incremented", "added": 144, "total": 288}) as add, \
             patch.object(app, "log_event"):
            x=self.client.post("/api/scan",json=self.payload())
            y=self.client.post("/api/scan",json=self.payload())
        self.assertEqual(x.status_code, 200, x.json)
        self.assertEqual(y.status_code, 200, y.json)
        self.assertEqual(add.call_count, 2)

    def test_timeout_retry_does_not_write_again_and_can_be_reconciled(self):
        p=self.payload()
        with patch.object(app, "find_product_pack_by_barcode", return_value=(self.product, self.pack)), \
             patch.object(app, "add_or_increment_position", side_effect=TimeoutError("network lost")) as add:
            first=self.client.post("/api/scan",json=p)
            repeated=self.client.post("/api/scan",json=p)
        self.assertEqual(first.status_code, 503, first.json)
        self.assertEqual(repeated.status_code, 409, repeated.json)
        add.assert_called_once()
        status=self.client.get("/api/scan-operation/"+p["operation_id"])
        self.assertEqual(status.json["status"], "uncertain")
        self.assertEqual(self.client.post("/api/receipts/receipt-1/complete").status_code,409)
        resolve=self.client.post(
            "/api/scan-operation/"+p["operation_id"]+"/reconcile",json={"verified":True}
        )
        self.assertEqual(resolve.status_code,200,resolve.json)
        self.assertEqual(resolve.json["status"],"reconciled")

    def test_uuid_cannot_be_reused_for_another_receipt_or_quantity(self):
        p=self.payload()
        with patch.object(app, "find_product_pack_by_barcode", return_value=(self.product, self.pack)), \
             patch.object(app, "add_or_increment_position", return_value={"mode": "created", "added":144,"total":144}) as add, \
             patch.object(app,"log_event"):
            self.assertEqual(self.client.post("/api/scan",json=p).status_code,200)
            other=dict(p, receipt_id="another-receipt")
            qty=dict(p, quantity=100)
            self.assertEqual(self.client.post("/api/scan",json=other).status_code,409)
            self.assertEqual(self.client.post("/api/scan",json=qty).status_code,409)
        add.assert_called_once()

    def test_scan_requires_operation_id(self):
        p=self.payload()
        del p["operation_id"]
        self.assertEqual(self.client.post("/api/scan",json=p).status_code,400)

    def test_old_html_no_longer_directly_adds_before_confirm(self):
        html=app.PAGE
        self.assertIn("✓ Подтвердить 1 палету",html)
        self.assertIn("async function confirmStagedPallet()",html)
        self.assertIn("localStorage.setItem(RECEIVING_PENDING_KEY",html)
        self.assertIn("async function checkReceivingPallet()",html)


if __name__=="__main__":
    unittest.main()
