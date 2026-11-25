"""Synthetic, read-only tests for the WB remainder manager screen."""
import base64
import unittest
from unittest.mock import patch

from flask import Flask
import wb_unpicked_manager as module


# Valid tiny PNG: original file mock, NOT a WB barcode or printable sample.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lZkAAAAASUVORK5CYII="
)


class UnpickedManagerTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "synthetic-test-only"
        self.app.register_blueprint(module.wb_unpicked_bp)
        self.client = self.app.test_client()

    def test_real_sqlite_queue_listing_and_filters(self):
        """Run actual list SQL against an isolated synthetic SQLite file."""
        import tempfile
        from pathlib import Path
        import wms
        from wb_fbs_sync_v11 import init_schema
        with tempfile.TemporaryDirectory(prefix="pr177-synthetic-") as tmp:
            with patch.object(wms, "DB_PATH", str(Path(tmp) / "synthetic.db")):
                init_schema()
                def add(oid, account, wb, local, queued, article):
                    wms.execute("""INSERT INTO wb_fbs_orders
                      (account_id,order_id,nm_id,seller_article,wb_status,local_status,synced_at)
                      VALUES(?,?,?,?,?,?,?)""",
                      (account, oid, "1863266481", article, wb, local, "synthetic"))
                    if queued:
                        wms.execute("""INSERT INTO wb_fbs_picking_queue
                          (account_id,order_id,accepted_at) VALUES(?,?,?)""",
                          (account, oid, "synthetic"))
                add("101", "default", "confirm", "pending", True, "ART-101")
                add("102", "default", "complete", "pending", True, "ART-102")
                add("103", "default", "complete", "complete", True, "ART-103")
                add("104", "default", "cancel", "pending", True, "ART-104")
                add("105", "other", "confirm", "pending", True, "ART-105")
                add("106", "default", "complete", "pending", False, "ART-106")
                for sku in ("4600000000001", "4600000000002"):
                    wms.execute("""INSERT INTO wb_fbs_order_skus
                      (account_id,order_id,sku) VALUES(?,?,?)""",
                      ("default", "101", sku))
                total, rows = module._read_page("default", "all", "", 1)
                self.assertEqual(total, 2)
                self.assertEqual({r["order_id"] for r in rows}, {"101", "102"})
                self.assertEqual(len(next(r for r in rows if r["order_id"] == "101")["product_barcodes"]), 2)
                self.assertEqual(module._read_page("default", "confirm", "", 1)[0], 1)
                self.assertEqual(module._read_page("default", "complete", "", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "ART-101", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "4600000000002", 1)[0], 1)
                self.assertEqual(module._read_page("default", "all", "1863266481", 1)[0], 2)
                self.assertEqual(module._read_page("default", "all", "nonexistent", 1)[0], 0)

    def test_article_groups_use_entire_queue_not_first_50_orders(self):
        import tempfile
        from pathlib import Path
        import wms
        from wb_fbs_sync_v11 import init_schema
        with tempfile.TemporaryDirectory(prefix="pr178-grouped-") as tmp:
            with patch.object(wms, "DB_PATH", str(Path(tmp) / "synthetic.db")):
                init_schema()
                for i in range(125):
                    oid = str(10000 + i)
                    article = "952_OTVODKA" if i < 105 else "2346_GRAFIT"
                    status = "complete" if i % 7 == 0 else "confirm"
                    wms.execute("""INSERT INTO wb_fbs_orders
                      (account_id,order_id,nm_id,seller_article,wb_status,local_status,synced_at)
                      VALUES(?,?,?,?,?,?,?)""",
                      ("default", oid, "123456", article, status, "pending", "synthetic"))
                    wms.execute("""INSERT INTO wb_fbs_picking_queue
                      (account_id,order_id,accepted_at) VALUES(?,?,?)""",
                      ("default", oid, "synthetic"))
                wms.execute("""INSERT INTO wb_fbs_orders
                  (account_id,order_id,seller_article,wb_status,local_status,synced_at)
                  VALUES(?,?,?,?,?,?)""",
                  ("default", "20001", "952_OTVODKA", "complete", "complete", "synthetic"))
                wms.execute("""INSERT INTO wb_fbs_orders
                  (account_id,order_id,seller_article,wb_status,local_status,synced_at)
                  VALUES(?,?,?,?,?,?)""",
                  ("other", "20002", "952_OTVODKA", "confirm", "pending", "synthetic"))
                for oid, acc in (("20001","default"),("20002","other")):
                    wms.execute("""INSERT INTO wb_fbs_picking_queue
                      (account_id,order_id,accepted_at) VALUES(?,?,?)""",
                      (acc, oid, "synthetic"))
                group_count, order_count, groups = module._read_groups("default", "all", "", 1)
                self.assertEqual(group_count, 2)
                self.assertEqual(order_count, 125)
                self.assertEqual([(x["article"], x["count"]) for x in groups],
                                 [("952_OTVODKA", 105), ("2346_GRAFIT", 20)])
                self.assertEqual(groups[0]["confirm"] + groups[0]["delivery"], 105)
                for pg, expected in ((1,50),(2,50),(3,5)):
                    total, items = module._read_page("default","all","",pg,"952_OTVODKA")
                    self.assertEqual(total, 105)
                    self.assertEqual(len(items), expected)
                    self.assertTrue(all(x["seller_article"]=="952_OTVODKA" for x in items))
                self.assertEqual(module._read_groups("default","complete","",1)[1],
                                 sum(1 for i in range(125) if i%7==0))
                self.assertEqual(module._read_groups("default","all","GRAFIT",1)[1],20)
                self.assertEqual(module._read_groups("default","all","952_OTVODKA",1)[1],105)
                self.assertEqual(module._read_groups("default","all","952%OTVODKA",1)[1],0)
                self.assertEqual(module._read_page("default","all","952_OTVODKA",1)[0],105)
                self.assertEqual(module._read_page("default","all","952%OTVODKA",1)[0],0)
                self.assertEqual(module._search_pattern("a_b%c!d"), "%a!_b!%c!!d%")
                self.assertEqual(module._read_groups("default","all","nothing",1)[1],0)

    def test_wb_order_day_uses_moscow_and_unknown_is_explicit(self):
        self.assertEqual(module._order_day("2026-11-23T22:30:00Z"), "2026-11-24")
        self.assertEqual(module._order_day("2026-11-24T01:30:00+03:00"), "2026-11-24")
        self.assertEqual(module._order_day("2026-11-24T12:00:00+00:00"), "2026-11-24")
        self.assertEqual(module._order_day(""), "unknown")
        self.assertEqual(module._order_day("nonsense"), "unknown")
        self.assertEqual(module._order_day(None), "unknown")
        self.assertEqual(module._assembly_day("2026-11-24T23:40:00Z"), "2026-11-25")

    def test_admission_day_not_order_day_on_synthetic_queue(self):
        import tempfile
        from pathlib import Path
        import wms
        from wb_fbs_sync_v11 import init_schema
        with tempfile.TemporaryDirectory(prefix="wb-entry-days-synthetic-") as tmp:
            with patch.object(wms, "DB_PATH", str(Path(tmp) / "synthetic.db")):
                init_schema()

                def add(oid, created, accepted=None, status="confirm",
                        local="pending", article="ART", account="default",
                        queued=True):
                    wms.execute("""INSERT INTO wb_fbs_orders
                        (account_id,order_id,nm_id,seller_article,wb_status,
                         local_status,created_at,synced_at)
                        VALUES(?,?,?,?,?,?,?,?)""",
                        (account,oid,"100",article,status,local,created,
                         "2026-11-25T10:00:00Z"))
                    if queued:
                        wms.execute("""INSERT INTO wb_fbs_picking_queue
                          (account_id,order_id,accepted_at) VALUES(?,?,?)""",
                          (account,oid,accepted or "2026-11-25T07:00:00Z"))

                # Creation date and first observed assembly date are deliberately different.
                add("101","2026-11-23T22:30:00Z",
                    "2026-11-25T08:00:00Z",article="A")
                add("102","2026-11-24T00:15:00Z",
                    "2026-11-25T08:10:00Z",status="complete",article="A")
                add("103","2026-11-23T12:00:00Z",
                    "2026-11-24T17:00:00Z",article="B")
                add("104",None,"2026-11-25T08:30:00Z",
                    status="complete",article="C")
                add("105","2026-11-24T04:00:00Z",local="complete")
                add("106","2026-11-24T04:00:00Z",status="cancel")
                add("107","2026-11-24T04:00:00Z",account="other")
                add("108","2026-11-24T04:00:00Z",queued=False)

                days=module._read_days("default","all","")
                self.assertEqual(sum(x["count"] for x in days),5)
                self.assertEqual([(x["day"],x["count"]) for x in days],
                                 [("2026-11-25",3),("2026-11-24",1),("unknown",1)])
                self.assertEqual(days[0]["confirm"],1)
                self.assertEqual(days[0]["delivery"],2)
                self.assertEqual(module._read_groups("default","all","",1,"2026-11-25")[1],3)
                self.assertEqual(module._read_groups("default","all","",1,"2026-11-24")[1],1)
                self.assertEqual(module._read_groups("default","all","",1,"unknown")[1],1)
                self.assertEqual(module._read_page("default","all","",1,"A","2026-11-25")[0],2)
                self.assertEqual(module._read_page("default","all","",1,None,"unknown")[0],1)
                self.assertEqual(module._read_page("default","all","",1,None,"2026-11-23")[0],0)
                self.assertEqual(module._read_page("default","confirm","",1,None,"2026-11-25")[0],1)
                self.assertEqual(module._read_page("default","complete","",1,None,"2026-11-25")[0],2)
                self.assertEqual(module._read_days("default","confirm","")[0]["count"],1)
                self.assertEqual(module._read_days("default","all","ZZZ"),[])
                # Dashboard includes unadmitted current CONFIRM, but print still blocks them.
                self.assertEqual(module._read_page("default","confirm","",1,None,"unknown")[0],1)
                self.assertEqual(wms.query_one(
                    "SELECT COUNT(*) FROM wb_fbs_picking_queue")[0],7)

    def test_days_api_requires_manager_and_validates_day_filter(self):
        with patch.object(module, "manager_access", return_value=False):
            self.assertEqual(self.client.get("/api/manager/wb-fbs/unpicked/days").status_code,403)
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "_read_days", return_value=[
                 {"day":"2026-11-24","count":2,"confirm":1,"delivery":1}]):
            r=self.client.get("/api/manager/wb-fbs/unpicked/days")
            self.assertEqual(r.status_code,200)
            self.assertEqual(r.json["orders"],2)
            for bad in ("bogus","2026-02-30","2026-1-2"):
                self.assertEqual(self.client.get("/api/manager/wb-fbs/unpicked/groups?day="+bad).status_code,400)
                self.assertEqual(self.client.get("/api/manager/wb-fbs/unpicked?day="+bad).status_code,400)

    def test_day_panel_keeps_article_and_order_drilldown(self):
        html=module.PAGE
        self.assertIn('id="daysPanel"',html)
        self.assertIn('id="allDays"',html)
        self.assertIn('selectedDay',html)
        self.assertIn("loadDays();loadGroups();",html)
        self.assertIn("Впервые на сборке (МСК)",html)

    def test_groups_api_manager_access(self):
        with patch.object(module, "manager_access", return_value=False):
            self.assertEqual(
                self.client.get("/api/manager/wb-fbs/unpicked/groups").status_code,403)
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "_read_groups", return_value=(1,3,[
                 {"article":"952_OTVODKA","count":3,"confirm":2,"delivery":1}
             ])):
            response=self.client.get("/api/manager/wb-fbs/unpicked/groups")
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json["groups"][0]["count"],3)
            self.assertEqual(response.json["orders"],3)

    def test_panels_explicitly_show_active_table(self):
        html = module.PAGE
        self.assertIn("""$('groupsPanel').style.display='block';$('detailPanel').style.display='none';""", html)
        self.assertIn("""$('groupsPanel').style.display='none';$('detailPanel').style.display='block';loadOrders();""", html)

    def test_manager_gate(self):
        with patch.object(module, "manager_access", return_value=False):
            self.assertEqual(self.client.get("/manager/wb-fbs/unpicked").status_code, 403)
            self.assertEqual(self.client.get("/api/manager/wb-fbs/unpicked").status_code, 403)
            self.assertEqual(self.client.post("/api/manager/wb-fbs/unpicked/print").status_code, 403)

    def test_page_and_list_have_no_mutation(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "_read_page", return_value=(1, [{
                 "order_id": "123", "seller_article": "ART", "wb_status": "confirm",
                 "product_barcodes": ["4601234567890"], "sticker_available": True
             }])):
            page = self.client.get("/manager/wb-fbs/unpicked")
            self.assertIn(b"58", page.data)
            result = self.client.get("/api/manager/wb-fbs/unpicked").json
            self.assertEqual(result["total"], 1)
            self.assertEqual(result["rows"][0]["product_barcodes"], ["4601234567890"])

    def test_print_requires_csrf(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True):
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                                        json={"order_ids": ["123"]})
            self.assertEqual(response.status_code, 403)

    def test_print_rejects_unadmitted_orders(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[]):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"}, json={"order_ids": ["123"]})
            self.assertEqual(response.status_code, 409)

    def test_rejects_duplicate_ids_and_over_limit(self):
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            for ids in (["123", "123"], [str(i) for i in range(51)], ["not-a-number"]):
                response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                    headers={"X-GRIT-CSRF": "synthetic"}, json={"order_ids": ids})
                self.assertEqual(response.status_code, 400)

    def test_original_png_pdf_page_dimensions(self):
        import fitz
        class MockWB:
            def __init__(self, *args, **kwargs):
                pass
            def stickers(self, ids):
                return [{"orderId": int(oid), "file": base64.b64encode(PNG).decode()} for oid in ids]
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[("123",), ("456",)]), \
             patch.object(module, "WBClient", MockWB), \
             patch.dict(module.os.environ, {"WB_API_TOKEN": "synthetic-not-real"}):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"},
                json={"order_ids": ["123", "456"]})
            self.assertEqual(response.status_code, 200, response.data[:300])
            self.assertEqual(response.mimetype, "application/pdf")
            with fitz.open(stream=response.data, filetype="pdf") as pdf:
                self.assertEqual(len(pdf), 2)
                for page in pdf:
                    self.assertAlmostEqual(page.rect.width, 58 * 72 / 25.4, places=2)
                    self.assertAlmostEqual(page.rect.height, 40 * 72 / 25.4, places=2)
                    self.assertEqual(len(page.get_images()), 1)

    def test_partial_original_sticker_response_cannot_print(self):
        class MockWB:
            def __init__(self, *a, **kw):
                pass
            def stickers(self, ids):
                return [{"orderId": 123, "file": base64.b64encode(PNG).decode()}]
        with patch.object(module, "manager_access", return_value=True), \
             patch.object(module, "_enabled", return_value=True), \
             patch.object(module, "query_all", return_value=[("123",), ("456",)]), \
             patch.object(module, "WBClient", MockWB), \
             patch.dict(module.os.environ, {"WB_API_TOKEN": "synthetic-not-real"}):
            with self.client.session_transaction() as sess:
                sess["wb_unpicked_print_csrf"] = "synthetic"
            response = self.client.post("/api/manager/wb-fbs/unpicked/print",
                headers={"X-GRIT-CSRF": "synthetic"},
                json={"order_ids": ["123", "456"]})
            self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
