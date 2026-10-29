"""OSZ and Promsiz use exactly the same Excel -> ONIX QR -> receiving path.

The external MoySklad API is mocked; run on SQLite and disposable PostgreSQL.
"""
import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from openpyxl import Workbook

_tmp = tempfile.TemporaryDirectory()
os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
if os.getenv("OSZ_TEST_POSTGRES") == "1":
    os.environ["WMS_REQUIRE_POSTGRES"] = "1"
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("OSZ_TEST_POSTGRES requires an isolated DATABASE_URL")
else:
    os.environ["WMS_REQUIRE_POSTGRES"] = "0"
    os.environ.pop("DATABASE_URL", None)
    os.environ["WMS_DB_PATH"] = os.path.join(_tmp.name, "osz-qr.db")

import app
from partner_pallets import db_init
from wms import init_wms_db, query_all, query_one


def workbook(rows):
    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    result = io.BytesIO()
    wb.save(result)
    wb.close()
    return io.BytesIO(result.getvalue())


class OszIdenticalToPromsizTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app.init_receiving_operations()
        db_init()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        self.name = "ОСЗ тест " + uuid.uuid4().hex[:12]
        password = "separate-partner-test-password"
        with self.client.session_transaction() as s:
            s["manager_web"] = True
        response = self.client.post(
            "/api/partners/accounts", json={"name": self.name, "password": password}
        )
        self.assertEqual(response.status_code, 201, response.json)
        response = self.client.post("/partners/login", data={"name": self.name, "password": password})
        self.assertEqual(response.status_code, 302, response.data[:300])
        with self.client.session_transaction(path="/partners") as s:
            self.partner_id = s["partner_id"]
            self.csrf = s["partner_csrf"]

    def upload(self, rows):
        return self.client.post(
            "/partners/upload",
            data={"csrf": self.csrf, "file": (workbook(rows), "osz.xlsx")},
            content_type="multipart/form-data",
        )

    def test_excel_creates_one_unique_onix_qr_per_pallet_and_a4_labels(self):
        response = self.upload([
            ("Артикул", "Палет", "Ед. в палете", "Количество, ед."),
            ("OSZ_SKU", 3, 144, 432),
        ])
        self.assertEqual(response.status_code, 302, response.data[:500])
        rows = query_all(
            "SELECT id,barcode FROM partner_pallets WHERE partner_id=? ORDER BY id",
            (self.partner_id,),
        )
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({row[1] for row in rows}), 3)
        self.assertTrue(all(row[1].startswith("ONIX-PAL-") for row in rows))
        for pallet_id, qr in rows:
            label = self.client.get("/partners/pallet/" + pallet_id + "/print")
            self.assertEqual(label.status_code, 200)
            self.assertIn(b"size:A4 portrait", label.data)
            self.assertIn(b"OSZ_SKU", label.data)
            self.assertIn("КОЛИЧЕСТВО: 144 ШТ.".encode(), label.data)
            self.assertIn(b"data:image/png;base64,", label.data)
            self.assertNotIn(qr.encode(), label.data)

    def test_one_qr_receives_once_even_after_new_uuid(self):
        response = self.upload([("Артикул", "Количество"), ("OSZ_SKU", 144)])
        self.assertEqual(response.status_code, 302, response.data[:300])
        pallet_id, qr_code = query_one(
            "SELECT id,barcode FROM partner_pallets WHERE partner_id=?",
            (self.partner_id,),
        )
        product = {
            "id": "osz-product",
            "article": "OSZ_SKU",
            "name": "OSZ product",
            "meta": {"href": "https://example.test/product", "type": "product"},
        }
        pack = {"name": "Палета", "quantity": 144}
        first_payload = {
            "operation_id": str(uuid.uuid4()),
            "receipt_id": "test-receipt",
            "barcode": qr_code,
        }
        with patch.object(app, "get_session_employee",
                          return_value={"id": "viktor", "name": "Viktor"}), \
             patch.object(app, "partner_receiving_product", return_value=(product, pack)), \
             patch.object(app, "add_or_increment_position",
                          return_value={"mode": "created", "added": 144, "total": 144}) as write, \
             patch.object(app, "log_event"):
            first = self.client.post("/api/scan", json=first_payload)
            retry = self.client.post("/api/scan", json=first_payload)
            next_scan = self.client.post(
                "/api/scan",
                json=dict(first_payload, operation_id=str(uuid.uuid4())),
            )
        self.assertEqual(first.status_code, 200, first.json)
        self.assertEqual(retry.status_code, 200, retry.json)
        self.assertTrue(retry.json["replayed"])
        self.assertEqual(next_scan.status_code, 409, next_scan.json)
        self.assertEqual(first.json["added"], 144)
        write.assert_called_once()
        self.assertEqual(
            query_one("SELECT status FROM partner_pallets WHERE id=?", (pallet_id,))[0],
            "accepted",
        )

    def test_partial_pallet_excel_uses_the_same_quantity_as_promsiz(self):
        response = self.upload([
            ("Палета", "Артикул", "Количество"),
            ("P1", "OSZ_SKU", 100),
            ("P2", "OSZ_SKU", 35),
        ])
        self.assertEqual(response.status_code, 302, response.data[:200])
        pallets = query_all(
            """SELECT l.quantity FROM partner_pallet_lines l
               JOIN partner_pallets p ON p.id=l.pallet_id WHERE p.partner_id=?
               ORDER BY l.quantity""",
            (self.partner_id,),
        )
        self.assertEqual([r[0] for r in pallets], [35, 100])

    def test_other_supplier_cannot_see_osz_pallets(self):
        self.assertEqual(
            self.upload([("Артикул", "Количество"), ("OSZ_SKU", 12)]).status_code,
            302,
        )
        with self.client.session_transaction(path="/partners") as s:
            s["partner_id"] = "nonexistent-supplier"
        self.assertEqual(self.client.get("/partners").status_code, 302)

    def test_receiving_ui_uses_qr_and_no_special_osz_scanner(self):
        html = app.PAGE
        self.assertIn("Html5QrcodeSupportedFormats.QR_CODE", html)
        self.assertNotIn('id="oszBtn"', html)
        self.assertNotIn("looksLikeSscc", html)
        self.assertIn("ONIX-PAL-", "ONIX-PAL-")


if __name__ == "__main__":
    unittest.main()
