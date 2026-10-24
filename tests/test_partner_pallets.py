import io
import os
import tempfile
import unittest
import uuid
from unittest.mock import patch

from openpyxl import Workbook

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_tmp = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_tmp.name, "partner.db")

import app
from partner_pallets import db_init
from wms import query_one

def workbook(rows):
    wb = Workbook()
    sheet = wb.active
    for row in rows:
        sheet.append(row)
    stream = io.BytesIO()
    wb.save(stream)
    return io.BytesIO(stream.getvalue())

class PartnerPalletTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db_init()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        self.name = "test_" + uuid.uuid4().hex
        with self.client.session_transaction() as s:
            s["manager_web"] = True
        response = self.client.post("/api/partners/accounts", json={"name": self.name, "password": "some-super-secure-password"})
        self.assertEqual(response.status_code, 201, response.json)
        self.client.post("/partners/login", data={"name": self.name, "password": "some-super-secure-password"})
        with self.client.session_transaction() as s:
            self.token = s["partner_csrf"]

    def upload(self, rows, name="pallets.xlsx"):
        return self.client.post("/partners/upload", data={"csrf": self.token, "file": (workbook(rows), name)},
                                content_type="multipart/form-data")

    def test_unique_pallets_grouping_and_history(self):
        r = self.upload([("Палета", "Артикул", "Количество"), ("P1", "SKU1", 10),
                         ("P1", "SKU2", 3), ("P2", "SKU3", 8)])
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as s:
            partner_id = s["partner_id"]
        rows = __import__("wms").query_all(
            "SELECT id,barcode FROM partner_pallets WHERE partner_id=?", (partner_id,))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({x[1] for x in rows}), 2)
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallet_lines WHERE pallet_id=?", (rows[0][0],))[0] in (1, 2), True)
        label = self.client.get(f"/partners/pallet/{rows[0][0]}/print")
        self.assertEqual(label.status_code, 200)
        self.assertIn(rows[0][1].encode(), label.data)
        self.assertIn(b"data:image/png;base64,", label.data)
        self.assertIn(b"size:A4 portrait", label.data)
        self.assertIn(b"width:210mm;height:297mm", label.data)

    def test_isolation_and_csrf(self):
        good = self.upload([("Артикул", "Количество"), ("SKU1", 4)])
        self.assertEqual(good.status_code, 302)
        with self.client.session_transaction() as s:
            pid = s["partner_id"]
        pallet_id = query_one("SELECT id FROM partner_pallets WHERE partner_id=?", (pid,))[0]
        with self.client.session_transaction() as s:
            s["partner_id"] = "not-my-id"
        self.assertEqual(self.client.get(f"/partners/pallet/{pallet_id}/print").status_code, 401)
        with self.client.session_transaction() as s:
            s["partner_id"] = pid
        bad = self.client.post("/partners/upload", data={"file": (workbook([("Артикул","Количество"),("SKU",1)]), "x.xlsx")},
                               content_type="multipart/form-data")
        self.assertEqual(bad.status_code, 403)

    def test_invalid_quantities_are_atomic(self):
        bad = self.upload([("Артикул", "Количество"), ("SKU1", 3), ("SKU2", -1)])
        self.assertEqual(bad.status_code, 400)
        with self.client.session_transaction() as s:
            pid = s["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?", (pid,))[0], 0)

if __name__ == "__main__":
    unittest.main()
