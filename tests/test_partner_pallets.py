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
        from fbs_catalog import init_fbs_catalog_db
        from wms import db_connect, sql
        init_fbs_catalog_db()
        with db_connect() as db:
            cur=db.cursor()
            for sku in ("SKU_NEW","SKU"):
                cur.execute(sql("INSERT INTO fbs_catalog_barcodes(barcode,product_id,article,name,quantity,is_pack,synced_at) VALUES(?,?,?,?,?,?,?)"),
                    ("TEST-"+sku,sku,sku,"Тестовый товар "+sku,1,0,"2026-10-24"))
            db.commit()
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

    def test_purchase_order_expands_66_pallets(self):
        rows = [
            ("Проект заказа", None, None, None, None),
            (None, None, None, None, None),
            ("Раздел", "Артикул", "Палет", "Ед. в палете", "Количество, ед."),
            ("Г/Н", "1256_BOROCCO", 6, 150, 900),
            ("Гладье", "8020_NONE", 14, 60, 840),
            ("Г/Н", "1649_BOROCCO", 46, 144, 6624),
            (None, "Всего", 66, None, 8364),
        ]
        result = self.upload(rows, "заказ.xlsx")
        self.assertEqual(result.status_code, 302)
        with self.client.session_transaction() as state:
            pid = state["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?", (pid,))[0], 66)
        self.assertEqual(query_one("""SELECT SUM(l.quantity) FROM partner_pallet_lines l
          JOIN partner_pallets p ON l.pallet_id=p.id WHERE p.partner_id=?""", (pid,))[0], 8364)

    def test_purchase_order_bad_totals_rejected(self):
        rows = [("Артикул", "Палет", "Ед. в палете", "Количество, ед."),
                ("SKU1", 3, 100, 299)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)

    def test_partner_cannot_open_manager_modules(self):
        self.assertEqual(self.client.get("/manager/partners").status_code, 302)
        self.assertEqual(self.client.get("/manager/warehouse-map").status_code, 302)
        self.assertEqual(self.client.post("/api/partners/accounts", json={"name":"intruder","password":"123456789012"}).status_code, 403)

    def test_partner_pallet_lookup_and_atomic_claim(self):
        from partner_pallets import resolve_receiving_pallet, claim_receiving_pallet, set_receiving_pallet_status
        upload = self.upload([("Артикул", "Количество"), ("SKU1", 24)])
        self.assertEqual(upload.status_code, 302)
        with self.client.session_transaction() as state:
            pid = state["partner_id"]
        code = query_one("SELECT barcode FROM partner_pallets WHERE partner_id=?", (pid,))[0]
        pallet = resolve_receiving_pallet(code)
        self.assertEqual(pallet["lines"], [{"article": "SKU1", "quantity": 24}])
        self.assertEqual(pallet["status"], "created")
        self.assertTrue(claim_receiving_pallet(pallet["id"]))
        self.assertFalse(claim_receiving_pallet(pallet["id"]))
        set_receiving_pallet_status(pallet["id"], "accepted")
        self.assertEqual(resolve_receiving_pallet(code)["status"], "accepted")
        self.assertIsNotNone(resolve_receiving_pallet(code)["accepted_at"])


    def test_partner_catalog_search_is_scoped_and_read_only(self):
        response=self.client.get("/partners/catalog/search?q=SKU_NEW")
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(any(x["article"]=="SKU_NEW" for x in response.json["items"]))
        response=self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"UNKNOWN_SKU","quantity":"3","reason":"test"})
        self.assertEqual(response.status_code,400)
        with self.client.session_transaction() as state:
            state.pop("partner_id",None)
        self.assertEqual(self.client.get("/partners/catalog/search?q=SKU_NEW").status_code,401)

    def test_manual_create_edit_and_audit(self):
        from wms import query_all
        r=self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"SKU_NEW","quantity":"120","reason":"Добавили при погрузке"})
        self.assertEqual(r.status_code,302,r.data[:500])
        with self.client.session_transaction() as state:
            pid=state["partner_id"]
        pallet=query_one("SELECT id FROM partner_pallets WHERE partner_id=?",(pid,))
        self.assertIsNotNone(pallet)
        url="/partners/pallet/"+pallet[0]+"/edit"
        self.assertEqual(self.client.get(url).status_code,200)
        changed=self.client.post(url,data={"csrf":self.token,"article":"SKU_NEW","quantity":"100","reason":"Фактически 100"})
        self.assertEqual(changed.status_code,302,changed.data[:500])
        self.assertEqual(query_one("SELECT quantity FROM partner_pallet_lines WHERE pallet_id=?",(pallet[0],))[0],100)
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallet_changes WHERE pallet_id=?",(pallet[0],))[0],2)
        __import__("partner_pallets").claim_receiving_pallet(pallet[0])
        self.assertEqual(self.client.post(url,data={"csrf":self.token,"article":"SKU_NEW","quantity":"99","reason":"Поздно"}).status_code,409)
        self.assertEqual(query_one("SELECT quantity FROM partner_pallet_lines WHERE pallet_id=?",(pallet[0],))[0],100)

    def test_manual_create_rejects_invalid_and_wrong_owner(self):
        bad=self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"SKU","quantity":"-3","reason":"Ошибка"})
        self.assertEqual(bad.status_code,400)
        with self.client.session_transaction() as state:
            pid=state["partner_id"]
        self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"SKU","quantity":"3","reason":"Добавлена"})
        pallet=query_one("SELECT id FROM partner_pallets WHERE partner_id=?",(pid,))[0]
        with self.client.session_transaction() as state:
            state["partner_id"]="another-partner"
        self.assertEqual(self.client.get("/partners/pallet/"+pallet+"/edit").status_code,401)

    def test_invalid_quantities_are_atomic(self):
        bad = self.upload([("Артикул", "Количество"), ("SKU1", 3), ("SKU2", -1)])
        self.assertEqual(bad.status_code, 400)
        with self.client.session_transaction() as s:
            pid = s["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?", (pid,))[0], 0)

if __name__ == "__main__":
    unittest.main()
