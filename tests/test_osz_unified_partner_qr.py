"""OSZ follows the identical partner-portal QR flow used for Promsiz.

SQLite integration tests with mocked MoySklad; no production data access.
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
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
os.environ["WMS_DB_PATH"] = os.path.join(_tmp.name, "osz-qr.db")

import app
from partner_pallets import db_init, legacy_sscc_pallet
from wms import db_connect, init_wms_db, query_all, query_one, sql


def workbook(rows):
    wb = Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    result = io.BytesIO()
    wb.save(result)
    wb.close()
    return io.BytesIO(result.getvalue())


def new_sscc():
    body = f"{uuid.uuid4().int % 10**17:017d}"
    total = sum(int(x) * (3 if i % 2 == 0 else 1)
                for i, x in enumerate(reversed(body)))
    return body + str((10 - total % 10) % 10)


class OszUnifiedQrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app.init_receiving_operations()
        db_init()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        self.supplier = "ОСЗ тест " + uuid.uuid4().hex[:12]
        self.password = "secure-osz-pallet-test-password"
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True
        create = self.client.post("/api/partners/accounts",
                                  json={"name": self.supplier, "password": self.password})
        self.assertEqual(create.status_code, 201, create.json)
        login = self.client.post("/partners/login",
                                 data={"name": self.supplier, "password": self.password})
        self.assertEqual(login.status_code, 302)
        with self.client.session_transaction(path="/partners") as sess:
            self.partner_id = sess["partner_id"]
            self.csrf = sess["partner_csrf"]

    def upload(self, rows):
        return self.client.post(
            "/partners/upload", data={"csrf": self.csrf,
                                      "file": (workbook(rows), "osz.xlsx")},
            content_type="multipart/form-data",
        )

    def test_osz_excel_produces_same_qr_as_promsiz_and_legacy_code_is_blocked(self):
        code = new_sscc()
        rows = [("Артикул", "Палет", "Ед. в палете", "Количество, ед.", "SSCC"),
                ("SKU_TEST_OSZ", 1, 144, 144, code)]
        created = self.upload(rows)
        self.assertEqual(created.status_code, 302, created.data[:400])
        pallets = query_all("SELECT id,barcode,status FROM partner_pallets WHERE partner_id=?",
                            (self.partner_id,))
        self.assertEqual(len(pallets), 1)
        pallet_id, qr_code, status = pallets[0]
        self.assertTrue(qr_code.startswith("ONIX-PAL-"))
        self.assertEqual(status, "created")
        self.assertEqual(legacy_sscc_pallet(code)[0], pallet_id)
        print_page = self.client.get("/partners/pallet/" + pallet_id + "/print")
        self.assertEqual(print_page.status_code, 200)
        self.assertIn(b"size:A4 portrait", print_page.data)
        self.assertIn("КОЛИЧЕСТВО: 144 ШТ.".encode(), print_page.data)
        self.assertNotIn(qr_code.encode(), print_page.data)
        old_check = self.client.post("/api/osz/check", json={"sscc": code})
        self.assertEqual(old_check.status_code, 409, old_check.json)
        self.assertEqual(old_check.json.get("reason"), "onix_qr_required")
        with patch.object(app, "get_session_employee",
                          return_value={"id": "viktor", "name": "Viktor"}), \
             patch.object(app, "add_or_increment_position") as write:
            old_accept = self.client.post(
                "/api/osz/accept", json={"sscc": code, "receipt_id": "rec-1",
                                         "product_barcode": "TEST", "quantity": 144}
            )
        self.assertEqual(old_accept.status_code, 409, old_accept.json)
        write.assert_not_called()

        product = {"id": "test-product", "article": "SKU_TEST_OSZ", "name": "OSZ",
                   "meta": {"href": "https://example.test/product", "type": "product"}}
        pack = {"name": "Палета", "quantity": 144}
        scan = {"operation_id": str(uuid.uuid4()), "receipt_id": "rec-1",
                "barcode": qr_code}
        with patch.object(app, "get_session_employee",
                          return_value={"id": "viktor", "name": "Viktor"}), \
             patch.object(app, "partner_receiving_product",
                          return_value=(product, pack)), \
             patch.object(app, "add_or_increment_position",
                          return_value={"mode": "created", "added": 144, "total": 144}) as write, \
             patch.object(app, "log_event"):
            accepted = self.client.post("/api/scan", json=scan)
            repeated = self.client.post(
                "/api/scan", json=dict(scan, operation_id=str(uuid.uuid4())))
        self.assertEqual(accepted.status_code, 200, accepted.json)
        self.assertEqual(repeated.status_code, 409, repeated.json)
        write.assert_called_once()
        self.assertEqual(query_one(
            "SELECT status FROM partner_pallets WHERE id=?", (pallet_id,)
        )[0], "accepted")
        self.assertEqual(legacy_sscc_pallet(code)[0], pallet_id)

    def test_osz_excel_without_old_sscc_is_same_as_promsiz(self):
        made = self.upload([("Артикул", "Палет", "Ед. в палете", "Количество, ед."),
                            ("SKU_TEST_OSZ", 2, 120, 240)])
        self.assertEqual(made.status_code, 302, made.data[:400])
        pallets = query_all(
            "SELECT id,barcode FROM partner_pallets WHERE partner_id=? ORDER BY id",
            (self.partner_id,))
        self.assertEqual(len(pallets), 2)
        self.assertEqual(len({r[1] for r in pallets}), 2)
        self.assertTrue(all(x[1].startswith("ONIX-PAL-") for x in pallets))
        self.assertEqual(query_one(
            "SELECT COUNT(*) FROM partner_pallet_legacy_sscc WHERE pallet_id IN (?,?)",
            (pallets[0][0], pallets[1][0]),
        )[0], 0)

    def test_osz_previous_legacy_receipt_cannot_be_recreated_as_new_qr(self):
        code = new_sscc()
        with db_connect() as conn:
            conn.execute(sql(
                "INSERT INTO external_pallets (sscc,partner,receipt_id,quantity,accepted_at)"
                " VALUES (?,?,?,?,?)"
            ), (code, "OSZ", "legacy-receipt", 144, "2026-10-28T08:00:00+00:00"))
            conn.commit()
        result = self.upload([("Артикул", "Количество", "SSCC"),
                              ("SKU_TEST_OSZ", 144, code)])
        self.assertEqual(result.status_code, 409, result.data[:400])
        self.assertIn("ранее обрабатывался".encode(), result.data)
        self.assertEqual(query_one(
            "SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?",
            (self.partner_id,)
        )[0], 0)
        self.assertIsNone(legacy_sscc_pallet(code))

    def test_osz_uncertain_legacy_attempt_cannot_be_relabelled(self):
        code = new_sscc()
        app.claim_operation(app.osz_receiving_operation_id(code), "rec-old",
                            "viktor", code, "144.0")
        result = self.upload([("Артикул", "Количество", "SSCC"),
                              ("SKU_TEST_OSZ", 144, code)])
        self.assertEqual(result.status_code, 409, result.data[:400])
        self.assertIsNone(legacy_sscc_pallet(code))

    def test_one_sscc_cannot_describe_multiple_new_pallets(self):
        code = new_sscc()
        result = self.upload([("Артикул", "Палет", "Ед. в палете", "SSCC"),
                              ("SKU_TEST_OSZ", 2, 144, code)])
        self.assertEqual(result.status_code, 400, result.data[:400])

    def test_partner_history_is_isolated(self):
        code = new_sscc()
        self.assertEqual(self.upload([("Артикул", "Количество", "SSCC"),
                                      ("SKU_TEST_OSZ", 12, code)]).status_code, 302)
        first = self.client.get("/partners").data.decode("utf-8")
        self.assertIn("SKU_TEST_OSZ", first)
        with self.client.session_transaction(path="/partners") as sess:
            sess["partner_id"] = "nonexisting-partner"
        other = self.client.get("/partners")
        self.assertEqual(other.status_code, 302)


if __name__ == "__main__":
    unittest.main()
