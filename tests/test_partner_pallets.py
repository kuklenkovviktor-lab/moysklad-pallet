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
        with self.client.session_transaction(path="/partners") as s:
            self.token = s["partner_csrf"]

    def upload(self, rows, name="pallets.xlsx"):
        return self.client.post("/partners/upload", data={"csrf": self.token, "file": (workbook(rows), name)},
                                content_type="multipart/form-data")

    def test_shipment_creation_grouping_batch_print_and_edit(self):
        from wms import query_all
        created=self.client.post("/partners/shipments/new",data={
            "csrf":self.token,"planned_date":"2026-10-27"})
        self.assertEqual(created.status_code,302,created.data[:300])
        sid=created.location.rsplit("/",1)[-1]
        self.assertIn("2026-10-27",self.client.get(created.location).data.decode())
        for qty in ("24","36"):
            r=self.client.post("/partners/pallet/new",data={
                "csrf":self.token,"shipment_id":sid,"article":"SKU_NEW",
                "quantity":qty,"reason":"Дополнение поставки"})
            self.assertEqual(r.status_code,302,r.data[:200])
        self.assertEqual(self.client.get(created.location).status_code,200)
        with self.client.session_transaction(path="/partners") as session:
            partner_id=session["partner_id"]
        rows=query_all("""SELECT id,barcode FROM partner_pallets
            WHERE partner_id=? AND upload_id=?""",(partner_id,sid))
        self.assertEqual(len(rows),2)
        batch=self.client.get(created.location+"/print")
        self.assertEqual(batch.status_code,200,batch.data[:200])
        self.assertEqual(batch.data.count(b'class="sheet"'),2)
        self.assertIn(b"size:A4 portrait",batch.data)
        self.assertNotIn(rows[0][1].encode(),batch.data)
        self.assertNotIn(rows[1][1].encode(),batch.data)
        self.assertEqual(self.client.get(created.location+"/print?only=created").status_code,200)
        edit=self.client.get("/partners/pallet/"+rows[0][0]+"/edit")
        self.assertEqual(edit.status_code,200)
        self.assertIn("Поставки",self.client.get("/partners").data.decode("utf-8"))

    def test_shipments_are_private_and_require_csrf(self):
        r=self.client.post("/partners/shipments/new",data={"planned_date":"2026-10-27"})
        self.assertEqual(r.status_code,403)
        r=self.client.post("/partners/shipments/new",data={
           "csrf":self.token,"planned_date":"2026-10-27"})
        sid=r.location.rsplit("/",1)[-1]
        with self.client.session_transaction(path="/partners") as session:
            pid=session["partner_id"]
            session["partner_id"]="other-partner"
        self.assertEqual(self.client.get("/partners/shipments/"+sid).status_code,404)
        self.assertEqual(self.client.get("/partners/shipments/"+sid+"/print").status_code,404)
        with self.client.session_transaction(path="/partners") as session:
            session["partner_id"]=pid

    def test_unique_pallets_grouping_and_history(self):
        r = self.upload([("Палета", "Артикул", "Количество"), ("P1", "SKU1", 10),
                         ("P1", "SKU2", 3), ("P2", "SKU3", 8)])
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction(path="/partners") as s:
            partner_id = s["partner_id"]
        rows = __import__("wms").query_all(
            "SELECT id,barcode FROM partner_pallets WHERE partner_id=?", (partner_id,))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({x[1] for x in rows}), 2)
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallet_lines WHERE pallet_id=?", (rows[0][0],))[0] in (1, 2), True)
        label = self.client.get(f"/partners/pallet/{rows[0][0]}/print")
        self.assertEqual(label.status_code, 200)
        self.assertNotIn(rows[0][1].encode(), label.data)
        self.assertNotIn("Добавлена вручную".encode(), label.data)
        self.assertIn(b"1/1", label.data)
        self.assertNotIn(b"class=\"code\"", label.data)
        self.assertIn(b"data:image/png;base64,", label.data)
        self.assertIn(b"size:A4 portrait", label.data)
        self.assertIn(b"width:210mm;height:297mm", label.data)


    def test_excel_labels_show_article_and_fraction_only(self):
        from partner_pallets import partner_pallet_display
        self.assertEqual(self.upload([("Артикул", "Палет", "Ед. в палете", "Количество, ед."),
            ("VIVA_96393", 2, 144, 288)]).status_code,302)
        with self.client.session_transaction(path="/partners") as state:
            partner_id=state["partner_id"]
        from wms import query_all
        pallets=query_all("SELECT id,barcode FROM partner_pallets WHERE partner_id=? ORDER BY created_at,id",(partner_id,))
        labels=partner_pallet_display(partner_id)
        self.assertEqual([labels[pid]["number"] for pid,_ in pallets],["1/2","2/2"])
        self.assertEqual({labels[pid]["article"] for pid,_ in pallets},{"VIVA_96393"})
        html=self.client.get("/partners").data.decode("utf-8")
        self.assertNotIn("· строка",html)
        self.assertNotIn("Добавлена вручную",html)
        self.assertIn("VIVA_96393",html)
        for pid,code in pallets:
            with patch("partner_pallets.qrcode.make", wraps=__import__("qrcode").make) as qr:
                result=self.client.get("/partners/pallet/"+pid+"/print")
            self.assertEqual(result.status_code,200)
            qr.assert_called_once_with(code)
            self.assertIn(labels[pid]["number"].encode(),result.data)
            self.assertIn(b"VIVA_96393",result.data)
            self.assertNotIn(code.encode(),result.data)
            self.assertNotIn("строка".encode(),result.data)
            self.assertNotIn("Добавлена вручную".encode(),result.data)

    def test_manual_pallets_clean_label_and_grouping(self):
        from partner_pallets import partner_pallet_display
        from wms import query_all
        for qty in ("144","100"):
            result=self.client.post("/partners/pallet/new",data={
                "csrf":self.token,"article":"SKU_NEW","quantity":qty,"reason":"Корректировка погрузки"})
            self.assertEqual(result.status_code,302,result.data[:500])
        with self.client.session_transaction(path="/partners") as state:
            partner_id=state["partner_id"]
        pallets=query_all("SELECT id,barcode FROM partner_pallets WHERE partner_id=? ORDER BY created_at,id",(partner_id,))
        labels=partner_pallet_display(partner_id)
        self.assertEqual([labels[pid]["number"] for pid,_ in pallets],["1/2","2/2"])
        html=self.client.get("/partners").data.decode("utf-8")
        self.assertNotIn("Добавлена вручную",html)
        self.assertNotIn(pallets[0][1],html)
        label=self.client.get("/partners/pallet/"+pallets[0][0]+"/print")
        self.assertEqual(label.status_code,200)
        self.assertIn(b"SKU_NEW",label.data)
        self.assertIn(b"1/2",label.data)
        self.assertNotIn("Добавлена вручную".encode(),label.data)
        self.assertNotIn(pallets[0][1].encode(),label.data)
        # Receiving identity and data remain unchanged.
        from partner_pallets import resolve_receiving_pallet
        self.assertEqual(resolve_receiving_pallet(pallets[0][1])["lines"][0]["quantity"],144)

    def test_dashboard_ui_stats_statuses_mobile_and_owner_filter(self):
        one=self.upload([("Артикул","Количество"),("SKU_NEW",5)])
        self.assertEqual(one.status_code,302,one.data[:350])
        self.assertIn("result=uploaded&count=1",one.location)
        dashboard=self.client.get("/partners")
        self.assertEqual(dashboard.status_code,200,dashboard.data[:350])
        html=dashboard.data.decode("utf-8")
        for marker in ('class="topbar"','class="kpis"','class="forms-grid"',
                       'name="submission_key"','id="history-search"',
                       'class="table-wrap"','@media(max-width:620px)',
                       'Статус','Создана','Создать палету и QR'):
            self.assertIn(marker,html)
        self.assertIn('Партнёр: '+self.name,html)
        self.assertIn("SKU_NEW",html)
        searched=self.client.get("/partners?q=SKU_NEW&status=created")
        self.assertEqual(searched.status_code,200)
        self.assertIn("SKU_NEW",searched.data.decode("utf-8"))
        missing=self.client.get("/partners?q=NON_EXISTENT")
        self.assertEqual(missing.status_code,200)
        self.assertIn("Палеты не найдены",missing.data.decode("utf-8"))
        # A partner must never see another partner's pallet.
        another=self.client.get("/partners?q=FOREIGN")
        self.assertNotIn("FOREIGN",another.data.decode("utf-8").split("<tbody>")[1].split("</tbody>")[0])

    def test_manual_duplicate_form_token_is_idempotent(self):
        from wms import query_one
        token="unique-browser-submit-"+uuid.uuid4().hex
        payload={"csrf":self.token,"article":"SKU_NEW","quantity":"18",
                 "reason":"Добавлено при погрузке","submission_key":token}
        first=self.client.post("/partners/pallet/new",data=payload)
        second=self.client.post("/partners/pallet/new",data=payload)
        self.assertEqual(first.status_code,302)
        self.assertIn("result=created&pallet=",first.location)
        self.assertEqual(second.status_code,302)
        self.assertIn("result=duplicate",second.location)
        with self.client.session_transaction(path="/partners") as session:
            pid=session["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?",(pid,))[0],1)

    def test_excel_duplicate_form_token_is_idempotent(self):
        rows=[("Артикул","Количество"),("SKU_NEW",18)]
        token="upload-submit-"+uuid.uuid4().hex
        def send():
            return self.client.post("/partners/upload",
                data={"csrf":self.token,"submission_key":token,
                      "file":(workbook(rows),"shipment.xlsx")},
                content_type="multipart/form-data")
        first,again=send(),send()
        self.assertEqual(first.status_code,302)
        self.assertIn("result=uploaded",first.location)
        self.assertEqual(again.status_code,302)
        self.assertIn("result=duplicate",again.location)
        with self.client.session_transaction(path="/partners") as session:
            pid=session["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?",(pid,))[0],1)

    def test_isolation_and_csrf(self):
        good = self.upload([("Артикул", "Количество"), ("SKU1", 4)])
        self.assertEqual(good.status_code, 302)
        with self.client.session_transaction(path="/partners") as s:
            pid = s["partner_id"]
        pallet_id = query_one("SELECT id FROM partner_pallets WHERE partner_id=?", (pid,))[0]
        with self.client.session_transaction(path="/partners") as s:
            s["partner_id"] = "not-my-id"
        self.assertEqual(self.client.get(f"/partners/pallet/{pallet_id}/print").status_code, 302)
        with self.client.session_transaction(path="/partners") as s:
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
        with self.client.session_transaction(path="/partners") as state:
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
        # A partner-only browser has no manager cookie. Its own partner
        # cookie must not create or impersonate manager access.
        partner_only = app.app.test_client()
        login = partner_only.post("/partners/login",data={
            "name":self.name,"password":"some-super-secure-password"})
        self.assertEqual(login.status_code,302)
        self.assertEqual(partner_only.get("/manager/partners").status_code,302)
        self.assertEqual(partner_only.get("/manager/warehouse-map").status_code,302)
        self.assertEqual(partner_only.post("/api/partners/accounts",json={
            "name":"intruder","password":"123456789012"}).status_code,403)

    def test_independent_partner_and_manager_cookies(self):
        with self.client.session_transaction() as state:
            self.assertTrue(state.get("manager_web"))
            self.assertIsNone(state.get("partner_id"))
        with self.client.session_transaction(path="/partners") as state:
            self.assertTrue(state.get("partner_id"))
            self.assertIsNone(state.get("manager_web"))
        # Logging out the partner does NOT close the manager's PC panel.
        result=self.client.post("/partners/logout",data={"csrf":self.token})
        self.assertEqual(result.status_code,302)
        self.assertEqual(self.client.get("/manager").status_code,200)
        self.assertEqual(self.client.get("/partners").status_code,302)
        self.assertIn("/partners/login",self.client.get("/partners").location)
        self.assertEqual(self.client.get("/partners/catalog/search?q=SKU_NEW").status_code,401)

    def test_manager_login_persists_with_seven_day_lifetime(self):
        from datetime import datetime, timezone
        import time
        import wms
        with self.client.session_transaction() as state:
            state.clear()
        with patch.object(wms,"MANAGER_WEB_PASSWORD","example-test-password"):
            response=self.client.post("/api/manager/login",json={"password":"example-test-password"})
        self.assertEqual(response.status_code,200,response.json)
        with self.client.session_transaction() as state:
            self.assertTrue(state.permanent)
            self.assertTrue(state["manager_web"])
            self.assertLess(abs(time.time()-state["manager_login_at"]),30)
        from email.utils import parsedate_to_datetime
        cookie=[c for c in response.headers.getlist("Set-Cookie") if c.startswith("session=")][0]
        expires=cookie.split("Expires=",1)[1].split(";",1)[0]
        duration=(parsedate_to_datetime(expires)-datetime.now(timezone.utc)).total_seconds()
        self.assertGreater(duration,6*86400)
        self.assertLess(duration,8*86400)
        self.assertEqual(self.client.get("/partners").status_code,200)

    def test_expired_manager_cookie_cannot_reenter_manager(self):
        import time
        with self.client.session_transaction() as state:
            state["manager_web"]=True
            state["manager_login_at"]=time.time()-8*86400
            state.permanent=True
        response=self.client.get("/manager")
        self.assertEqual(response.status_code,302)
        self.assertIn("/manager-login",response.location)
        # Expiring a manager session must not touch the partner.
        self.assertEqual(self.client.get("/partners").status_code,200)

    def test_partner_pallet_lookup_and_atomic_claim(self):
        from partner_pallets import resolve_receiving_pallet, claim_receiving_pallet, set_receiving_pallet_status
        upload = self.upload([("Артикул", "Количество"), ("SKU1", 24)])
        self.assertEqual(upload.status_code, 302)
        with self.client.session_transaction(path="/partners") as state:
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


    def test_catalog_pack_quantity_unique_and_ambiguous(self):
        from wms import db_connect, sql
        with db_connect() as db:
            for suffix,sku,qty in (("A","PACK_ONE",144),("B","PACK_MIX",100),("C","PACK_MIX",120)):
                db.cursor().execute(sql("INSERT INTO fbs_catalog_barcodes(barcode,product_id,article,name,quantity,is_pack,synced_at) VALUES (?,?,?,?,?,?,?)"),
                    ("PACK-"+suffix,sku,sku,"Тест "+sku,qty,1,"2026-10-24"))
            db.commit()
        one=self.client.get("/partners/catalog/search?q=PACK_ONE")
        self.assertEqual(one.status_code,200,one.json)
        self.assertEqual(one.json["items"][0]["pack_qty"],144)
        mixed=self.client.get("/partners/catalog/search?q=PACK_MIX")
        self.assertIsNone(mixed.json["items"][0]["pack_qty"])
        html=self.client.get("/partners").data
        self.assertIn(b"exact.pack_qty",html)

    def test_partner_catalog_search_is_scoped_and_read_only(self):
        response=self.client.get("/partners/catalog/search?q=SKU_NEW")
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(any(x["article"]=="SKU_NEW" for x in response.json["items"]))
        response=self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"UNKNOWN_SKU","quantity":"3","reason":"test"})
        self.assertEqual(response.status_code,400)
        with self.client.session_transaction(path="/partners") as state:
            state.pop("partner_id",None)
        self.assertEqual(self.client.get("/partners/catalog/search?q=SKU_NEW").status_code,401)

    def test_manual_create_edit_and_audit(self):
        from wms import query_all
        r=self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"SKU_NEW","quantity":"120","reason":"Добавили при погрузке"})
        self.assertEqual(r.status_code,302,r.data[:500])
        with self.client.session_transaction(path="/partners") as state:
            pid=state["partner_id"]
        pallet=query_one("SELECT id FROM partner_pallets WHERE partner_id=?",(pid,))
        self.assertIsNotNone(pallet)
        url="/partners/pallet/"+pallet[0]+"/edit"
        edit_page=self.client.get(url)
        self.assertEqual(edit_page.status_code,200)
        self.assertIn("История корректировок",edit_page.data.decode("utf-8"))
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
        with self.client.session_transaction(path="/partners") as state:
            pid=state["partner_id"]
        self.client.post("/partners/pallet/new",data={"csrf":self.token,"article":"SKU","quantity":"3","reason":"Добавлена"})
        pallet=query_one("SELECT id FROM partner_pallets WHERE partner_id=?",(pid,))[0]
        with self.client.session_transaction(path="/partners") as state:
            state["partner_id"]="another-partner"
        self.assertEqual(self.client.get("/partners/pallet/"+pallet+"/edit").status_code,302)

    def test_invalid_quantities_are_atomic(self):
        bad = self.upload([("Артикул", "Количество"), ("SKU1", 3), ("SKU2", -1)])
        self.assertEqual(bad.status_code, 400)
        with self.client.session_transaction(path="/partners") as s:
            pid = s["partner_id"]
        self.assertEqual(query_one("SELECT COUNT(*) FROM partner_pallets WHERE partner_id=?", (pid,))[0], 0)

if __name__ == "__main__":
    unittest.main()
