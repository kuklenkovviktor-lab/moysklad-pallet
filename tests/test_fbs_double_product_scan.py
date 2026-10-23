"""Regression: double scanning a merchandise EAN must never count as a WB order."""
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TMP.name, "fbs-equal-barcode.sqlite")

from flask import Flask
import wms
from wms import execute, ensure_db, now_iso, query_one, wms_bp


class FbsDoubleProductScanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()
        execute("INSERT INTO employees (id, name, rate, target) VALUES (?, ?, ?, ?)",
                ("anton", "Антон", 2.25, 1600))
        cls.app = Flask(__name__)
        cls.app.secret_key = "test"
        cls.app.testing = True
        cls.app.register_blueprint(wms_bp)

    def setUp(self):
        self.shift_id = str(uuid.uuid4())
        stamp = now_iso()
        execute("INSERT INTO shifts (id, employee_id, started_at) VALUES (?, ?, ?)",
                (self.shift_id, "anton", stamp))
        execute("INSERT INTO activities (id, shift_id, employee_id, activity_type, started_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), self.shift_id, "anton", "picking", stamp))
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["employee_id"] = "anton"

    def send(self, wb_code, product_code="2038768443973"):
        return self.client.post("/api/fbs/scan", json={
            "shift_id": self.shift_id,
            "barcode": wb_code,
            "product_barcode": product_code,
            "article": "92_022_reana2_none",
            "product_name": "База РЕАНА №2 (92_022) ПРОЗРАЧНАЯ",
            "scan_type": "normal"
        })

    def counted(self):
        return query_one("SELECT COUNT(*) FROM scans WHERE shift_id=?", (self.shift_id,))[0]

    def test_anton_exact_double_scan_rejected_without_order(self):
        with patch.object(wms, "WB_STRICT_MATCHING", False):
            result = self.send("2038768443973")
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(result.json["reason"], "wb_same_as_product")
        self.assertIn("НЕ засчитан", result.json["error"])
        self.assertEqual(self.counted(), 0)
        event = query_one("""
            SELECT event_type, result FROM events
            WHERE shift_id = ? AND event_type = 'wb_same_as_product'
            ORDER BY created_at DESC LIMIT 1
        """, (self.shift_id,))
        self.assertIsNotNone(event)

    def test_matching_rejected_even_when_strict_enabled(self):
        with patch.object(wms, "WB_STRICT_MATCHING", True):
            result = self.send("2038768443973")
        self.assertEqual(result.status_code, 409, result.json)
        self.assertEqual(self.counted(), 0)

    def test_ordinary_separate_wb_barcode_still_counts(self):
        with patch.object(wms, "WB_STRICT_MATCHING", False):
            result = self.send("*DckIqDYy")
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(self.counted(), 1)
        row = query_one("SELECT barcode, product_barcode FROM scans WHERE shift_id=?",
                        (self.shift_id,))
        self.assertEqual(row, ("*DckIqDYy", "2038768443973"))

    def test_other_product_code_same_as_second_scan_rejected(self):
        response = self.send("4601234567890", product_code="4601234567890")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.counted(), 0)

    def test_real_order_duplicate_still_rejected_without_extra_count(self):
        self.assertEqual(self.send("*DckIqDYy").status_code, 200)
        result = self.send("*DckIqDYy")
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.counted(), 1)

    def test_fbs_page_has_immediate_double_product_scan_feedback(self):
        self.assertIn("if(code===p.barcode)", wms.FBS_PAGE)
        self.assertIn("ДВА РАЗА ОТСКАНИРОВАН ТОВАР", wms.FBS_PAGE)


if __name__ == "__main__":
    unittest.main()
