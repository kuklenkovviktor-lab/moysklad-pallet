"""Test safe SKU-to-cell assignment without stock mutations or live MoySklad writes."""
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
TMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(TMP.name, "cell-walk.db")

import app
from cell_audit import ensure_cell_audit_db
from wms import execute, init_wms_db, query_one


class CellWalkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        ensure_cell_audit_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client=app.app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["manager_web"]=True
        self.loc_id=str(uuid.uuid4())
        self.label="К-"+uuid.uuid4().hex[:6].upper()
        self.code="ONIX-LOC-"+uuid.uuid4().hex[:16].upper()
        execute("""INSERT INTO replenishment_locations(id,label,barcode,created_at,active)
                   VALUES (?,?,?,?,1)""",
                (self.loc_id,self.label,self.code,"2026-10-22T00:00:00Z"))
        self.item={"id":"item-1","article":"SALAT_3V1_NONE","name":"Салатники 3 в 1 ПРОЗРАЧНЫЙ"}

    def payload(self,barcode="4601234567890",article="",**kw):
        return dict(location=self.loc_id,barcode=barcode,article=article,**kw)

    def post_lookup(self,route,payload=None,product=None):
        with patch.object(app,"find_product_by_any_barcode",
                         return_value=((product if product is not None else self.item),None)):
            return self.client.post(route,json=payload or self.payload())

    def test_preview_then_confirm_creates_durable_mapping_and_map_tile(self):
        found=self.client.get("/api/manager/cell-audit/locate?code="+self.code)
        self.assertEqual(found.status_code,200,found.json)
        self.assertEqual(found.json["location"]["label"],self.label)
        self.assertIsNone(found.json["assigned"])
        preview=self.post_lookup("/api/manager/cell-audit/preview")
        self.assertEqual(preview.status_code,200,preview.json)
        self.assertEqual(preview.json["product"]["article"],self.item["article"])
        self.assertIsNone(preview.json["previous"])
        self.assertFalse(preview.json["stock_changed"])
        body=self.payload(confirmed_product_id="item-1",expected_current_id=None,replace=False)
        result=self.post_lookup("/api/manager/cell-audit/assign",body)
        self.assertEqual(result.status_code,200,result.json)
        self.assertFalse(result.json["replaced"])
        mapping=query_one("SELECT article,product_id FROM cell_sku_assignments WHERE location_id=?",
                          (self.loc_id,))
        self.assertEqual(mapping,("SALAT_3V1_NONE","item-1"))
        page=self.client.get("/api/manager/warehouse-map")
        self.assertEqual(page.status_code,200,page.json)
        cell=next(c for c in page.json["cells"] if c["id"]==self.loc_id)
        self.assertEqual(cell["assignment"]["article"],"SALAT_3V1_NONE")
        self.assertEqual(cell["state"],"unknown") # assignment != proof of physical stock
        self.assertEqual(query_one("SELECT COUNT(*) FROM cell_sku_assignment_events WHERE location_id=?",
                                   (self.loc_id,))[0],1)

    def test_idempotent_reconfirm_does_not_duplicate_audit_events(self):
        body=self.payload(confirmed_product_id="item-1",expected_current_id=None,replace=False)
        self.assertEqual(self.post_lookup("/api/manager/cell-audit/assign",body).status_code,200)
        second=dict(body,expected_current_id="item-1")
        response=self.post_lookup("/api/manager/cell-audit/assign",second)
        self.assertEqual(response.status_code,200,response.json)
        self.assertTrue(response.json["unchanged"])
        self.assertEqual(query_one("SELECT COUNT(*) FROM cell_sku_assignment_events WHERE location_id=?",
                                   (self.loc_id,))[0],1)

    def test_no_silent_replace_and_stale_preview_rejected(self):
        first=self.payload(confirmed_product_id="item-1",expected_current_id=None,replace=False)
        self.assertEqual(self.post_lookup("/api/manager/cell-audit/assign",first).status_code,200)
        replacement={"id":"item-2","article":"OTHER_SKU","name":"Другой товар"}
        fresh=self.payload(confirmed_product_id="item-2",expected_current_id="item-1",replace=False)
        denied=self.post_lookup("/api/manager/cell-audit/assign",fresh,product=replacement)
        self.assertEqual(denied.status_code,409,denied.json)
        stale=self.post_lookup("/api/manager/cell-audit/assign",
                               dict(fresh,expected_current_id=None,replace=True),product=replacement)
        self.assertEqual(stale.status_code,409,stale.json)
        allowed=self.post_lookup("/api/manager/cell-audit/assign",
                                 dict(fresh,replace=True),product=replacement)
        self.assertEqual(allowed.status_code,200,allowed.json)
        self.assertTrue(allowed.json["replaced"])
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (self.loc_id,))[0],"OTHER_SKU")
        self.assertEqual(query_one("SELECT COUNT(*) FROM cell_sku_assignment_events WHERE location_id=?",
                                   (self.loc_id,))[0],2)

    def test_manual_exact_article_fallback_is_verified(self):
        manual=self.payload(barcode="",article="SALAT_3V1_NONE")
        with patch.object(app,"_get_product_list",return_value=[self.item]):
            p=self.client.post("/api/manager/cell-audit/preview",json=manual)
            self.assertEqual(p.status_code,200,p.json)
            self.assertEqual(p.json["product"]["method"],"article")
            b=dict(manual,confirmed_product_id="item-1",expected_current_id=None,replace=False)
            out=self.client.post("/api/manager/cell-audit/assign",json=b)
            self.assertEqual(out.status_code,200,out.json)
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (self.loc_id,))[0],"SALAT_3V1_NONE")

    def test_unknown_product_or_location_never_writes(self):
        unknown=self.post_lookup("/api/manager/cell-audit/preview",
                                 dict(self.payload(), location="NO-CELL"))
        self.assertEqual(unknown.status_code,404)
        with patch.object(app,"find_product_by_any_barcode",return_value=(None,None)):
            bad=self.client.post("/api/manager/cell-audit/preview",json=self.payload())
        self.assertEqual(bad.status_code,404)
        a=self.post_lookup("/api/manager/cell-audit/assign",self.payload(confirmed_product_id="WRONG",expected_current_id=None))
        self.assertEqual(a.status_code,409)
        self.assertIsNone(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",(self.loc_id,)))

    def test_employee_badge_cannot_access_or_change_manager_assignments(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["employee_id"]="anton"
        self.assertEqual(self.client.get("/api/manager/cell-audit/locate?code="+self.code).status_code,403)
        self.assertEqual(self.client.get("/api/manager/cell-audit/assignments").status_code,403)
        self.assertEqual(self.client.post("/api/manager/cell-audit/preview",json=self.payload()).status_code,403)
        self.assertEqual(self.client.post("/api/manager/cell-audit/assign",
             json=self.payload(confirmed_product_id="item-1",expected_current_id=None)).status_code,403)
        self.assertEqual(self.client.get("/manager/cell-audit").status_code,302)

    def _assign_for_move(self):
        body=self.payload(confirmed_product_id="item-1",expected_current_id=None,replace=False)
        response=self.post_lookup("/api/manager/cell-audit/assign",body)
        self.assertEqual(response.status_code,200,response.json)

    def _destination(self):
        dest_id=str(uuid.uuid4())
        dest_label="К-"+uuid.uuid4().hex[:6].upper()
        dest_code="ONIX-LOC-"+uuid.uuid4().hex[:16].upper()
        execute("""INSERT INTO replenishment_locations(id,label,barcode,created_at,active)
                   VALUES (?,?,?,?,1)""",(dest_id,dest_label,dest_code,"2026-10-22T00:00:00Z"))
        return dest_id,dest_label

    def test_manager_moves_existing_item_to_empty_cell_and_updates_map(self):
        self._assign_for_move()
        dest,label=self._destination()
        r=self.client.post("/api/manager/cell-audit/move",
            json={"source":self.loc_id,"target":label,"expected_product_id":"item-1"})
        self.assertEqual(r.status_code,200,r.json)
        self.assertFalse(r.json["stock_changed"])
        self.assertIsNone(query_one("SELECT product_id FROM cell_sku_assignments WHERE location_id=?",
                                    (self.loc_id,)))
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (dest,))[0],"SALAT_3V1_NONE")
        self.assertEqual(query_one("SELECT COUNT(*) FROM cell_sku_assignment_events WHERE kind IN ('move_out','move_in') AND location_id IN (?,?)",
                                   (self.loc_id,dest))[0],2)
        page=self.client.get("/api/manager/warehouse-map")
        self.assertEqual(page.status_code,200,page.json)
        by_id={cell["id"]:cell for cell in page.json["cells"]}
        self.assertIsNone(by_id[self.loc_id]["assignment"])
        self.assertEqual(by_id[dest]["assignment"]["article"],"SALAT_3V1_NONE")

    def test_move_rejects_occupied_destination_or_stale_source(self):
        self._assign_for_move()
        dest,label=self._destination()
        execute("""INSERT INTO cell_sku_assignments
            (location_id,product_id,article,product_name,source_barcode,assigned_at,assigned_by)
            VALUES (?,?,?,?,?,?,?)""",
            (dest,"other-product","OTHER","Another","", "2026-11-18T00:00:00Z","manager"))
        payload={"source":self.loc_id,"target":dest,"expected_product_id":"item-1"}
        r=self.client.post("/api/manager/cell-audit/move",json=payload)
        self.assertEqual(r.status_code,409,r.json)
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (self.loc_id,))[0],"SALAT_3V1_NONE")
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (dest,))[0],"OTHER")
        execute("DELETE FROM cell_sku_assignments WHERE location_id=?",(dest,))
        stale=self.client.post("/api/manager/cell-audit/move",json={
            **payload,"expected_product_id":"stale"})
        self.assertEqual(stale.status_code,409)
        self.assertIsNone(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                    (dest,)))

    def test_move_blocks_non_manager_and_invalid_destinations(self):
        self._assign_for_move()
        dest,label=self._destination()
        payload={"source":self.loc_id,"target":dest,"expected_product_id":"item-1"}
        self.assertEqual(self.client.post("/api/manager/cell-audit/move",
            json={**payload,"target":self.loc_id}).status_code,400)
        self.assertEqual(self.client.post("/api/manager/cell-audit/move",
            json={**payload,"target":"unknown"}).status_code,404)
        with self.client.session_transaction() as session:
            session.clear()
            session["employee_id"]="anton"
        forbidden=self.client.post("/api/manager/cell-audit/move",json=payload)
        self.assertEqual(forbidden.status_code,403)
        self.assertEqual(forbidden.json["error"],"Требуется вход в ПК-панель")
        self.assertIsNone(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                    (dest,)))
        self.assertEqual(query_one("SELECT article FROM cell_sku_assignments WHERE location_id=?",
                                   (self.loc_id,))[0],"SALAT_3V1_NONE")

    def test_location_id_link_and_mobile_page(self):
        url="/api/manager/cell-audit/locate?code="+self.loc_id
        r=self.client.get(url)
        self.assertEqual(r.status_code,200,r.json)
        html=self.client.get("/manager/cell-audit")
        self.assertEqual(html.status_code,200)
        text=html.get_data(as_text=True)
        self.assertIn("Обход ячеек",text)
        self.assertIn("Html5Qrcode",text)
        self.assertIn("location_id",text)

if __name__=="__main__":
    unittest.main()
