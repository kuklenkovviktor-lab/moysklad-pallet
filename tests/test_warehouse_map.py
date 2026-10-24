"""Warehouse map: tiles follow real locations and task state, never inventory inference."""
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TMP.name, "warehouse-map.db")

import app
from warehouse_map import (cell_state, line_of, location_sort_key,
                           WAREHOUSE_MAP_PAGE)
from wms import execute, init_wms_db, query_one


class WarehouseMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess["manager_web"] = True
        self.stamp = datetime.now(timezone.utc)
        self.tag = str(int(uuid.uuid4().hex[:6], 16))

    def loc(self, line="К", number=None):
        code = self.tag if number is None else number
        label = f"{line}-{code}"
        loc_id = str(uuid.uuid4())
        execute("""
            INSERT INTO replenishment_locations (id, label, barcode, created_at, active)
            VALUES (?, ?, ?, ?, 1)
        """, (loc_id, label, "ONIX-LOC-" + uuid.uuid4().hex[:16].upper(),
              self.stamp.isoformat()))
        return loc_id, label

    def task(self, label, status, article="SKU", priority="normal",
             age_minutes=1, completed_qty=None, assignee=None):
        id_ = str(uuid.uuid4())
        timestamp = (self.stamp - timedelta(minutes=age_minutes)).isoformat()
        completed_at = self.stamp.isoformat() if status == "completed" else None
        started_at = timestamp if status == "started" else None
        execute("""
            INSERT INTO replenishment_tasks (
              id, article, line_code, planned_qty, completed_qty, priority,
              note, status, created_at, started_at, completed_at, created_by,
              assigned_employee_id, product_name, verify_required,
              verified_product_at, verified_location_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (id_, article, label, 120, completed_qty, priority, "4 склад",
              status, timestamp, started_at, completed_at,
              "Тест", assignee, "Салатники", 1,
              timestamp if status == "completed" else None,
              timestamp if status == "completed" else None))
        return id_

    def test_bulk_print_all_250_cells_on_14_a4_pages_without_changing_codes(self):
        from unittest.mock import patch
        import replenishment_checks
        import replenishment
        import qrcode
        entries = [("А-"+str(n).zfill(3),"ONIX-LOC-TEST"+str(n).zfill(5))
                   for n in range(1,251)]
        generated=[]
        qr_type=qrcode.QRCode
        def record_qr(*args,**kwargs):
            qr=qr_type(*args,**kwargs)
            generated.append(qr)
            return qr
        with patch.object(replenishment_checks,"query_all",return_value=entries) as read, \
             patch.object(qrcode,"QRCode",side_effect=record_qr):
            page=self.client.get("/manager/replenishment/labels/all")
        self.assertEqual(page.status_code,200)
        self.assertIn("no-store",page.headers.get("Cache-Control",""))
        html=page.data.decode("utf-8")
        self.assertEqual(html.count('class="cell-label"'),250)
        self.assertEqual(html.count('class="sheet"'),14)
        self.assertIn("Активных ячеек: 250",html)
        self.assertIn("Листов А4: 14",html)
        self.assertIn("size:A4 portrait",html)
        self.assertIn("58mm",html)
        self.assertIn("40mm",html)
        self.assertIn("А-001",html)
        self.assertIn("А-250",html)
        self.assertEqual([x.data_list[0].data.decode("utf-8") for x in generated],
                         [code for _,code in entries])
        read.assert_called_once()
        self.assertIn("WHERE active = 1",read.call_args[0][0])
        self.assertIn('id="printAllLocations"',replenishment.MANAGER_PAGE)

    def test_bulk_print_manager_only_and_empty_or_excess_cells(self):
        from unittest.mock import patch
        import replenishment_checks
        with self.client.session_transaction() as sess:
            sess.clear()
        denied=self.client.get("/manager/replenishment/labels/all")
        self.assertEqual(denied.status_code,302)
        self.assertIn("/manager-login",denied.location)
        with self.client.session_transaction() as sess:
            sess["manager_web"]=True
        with patch.object(replenishment_checks,"query_all",return_value=[]):
            self.assertEqual(self.client.get("/manager/replenishment/labels/all").status_code,404)
        too_many=[("А-"+str(i),"ONIX-LOC-TEST"+str(i)) for i in range(1001)]
        with patch.object(replenishment_checks,"query_all",return_value=too_many):
            self.assertEqual(self.client.get("/manager/replenishment/labels/all").status_code,413)

    def test_bulk_print_uses_existing_active_codes_and_natural_order(self):
        from unittest.mock import patch
        import replenishment_checks
        import qrcode
        entries=[("А-10","ONIX-LOC-EXIST10"),("А-02","ONIX-LOC-EXIST02"),
                 ("Ж-01","ONIX-LOC-EXIST03")]
        qr_type=qrcode.QRCode
        captured=[]
        def spy_qr(*args,**kw):
            result=qr_type(*args,**kw)
            captured.append(result)
            return result
        with patch.object(replenishment_checks,"query_all",return_value=entries), \
             patch.object(qrcode,"QRCode",side_effect=spy_qr):
            result=self.client.get("/manager/replenishment/labels/all")
        self.assertEqual(result.status_code,200)
        self.assertEqual([x.data_list[0].data.decode() for x in captured],
                         ["ONIX-LOC-EXIST02","ONIX-LOC-EXIST10","ONIX-LOC-EXIST03"])

    def test_line_and_natural_sort_match_existing_addresses(self):
        self.assertEqual(line_of("К-07"), "К")
        self.assertEqual(line_of("Ж-01"), "Ж")
        self.assertEqual(line_of("И4"), "И")
        self.assertEqual(line_of("РЕЗЕРВ"), "ПРОЧЕЕ")
        locations = [{"label":x, "line":line_of(x)} for x in ("К-10", "К-02", "Ж-01", "А-01")]
        self.assertEqual([x["label"] for x in sorted(locations, key=location_sort_key)],
                         ["А-01", "Ж-01", "К-02", "К-10"])

    def test_statuses_depend_on_workflow_not_inferred_inventory(self):
        now = self.stamp
        task = {"status":"new","priority":"normal","created_at":now.isoformat(),
                "started_at":None}
        self.assertEqual(cell_state([], now=now), "unknown")
        self.assertEqual(cell_state([], last_complete={"id":"old"}, now=now), "completed")
        self.assertEqual(cell_state([task], now=now), "queued")
        self.assertEqual(cell_state([dict(task,status="started")], now=now), "started")
        self.assertEqual(cell_state([dict(task,status="blocked")], now=now), "blocked")
        self.assertEqual(cell_state([dict(task,priority="urgent")], now=now), "urgent")
        old = dict(task, created_at=(now-timedelta(minutes=31)).isoformat())
        self.assertEqual(cell_state([old], now=now), "delayed")
        self.assertEqual(cell_state([task, dict(task,status="blocked")], now=now), "queued")

    def test_map_reads_registered_cell_and_last_completed_test(self):
        loc_id, label = self.loc()
        self.task(label, "completed", article="SALAT_3V1_NONE",
                  completed_qty=120, assignee="anton")
        reply = self.client.get("/api/manager/warehouse-map")
        self.assertEqual(reply.status_code, 200, reply.json)
        self.assertFalse(reply.json["stock_is_authoritative"])
        tile = next(c for c in reply.json["cells"] if c["id"] == loc_id)
        self.assertEqual(tile["label"], label)
        self.assertEqual(tile["line"], "К")
        self.assertEqual(tile["state"], "completed")
        self.assertEqual(tile["tasks"], [])
        self.assertEqual(tile["last_task"]["completed_qty"], 120)
        self.assertEqual(tile["last_task"]["article"], "SALAT_3V1_NONE")

    def test_open_task_changes_tile_and_history_remains_visible(self):
        loc_id, label = self.loc("Ж")
        self.task(label, "completed", completed_qty=120)
        self.task(label, "new", article="NEW_SKU", priority="urgent")
        result = self.client.get("/api/manager/warehouse-map")
        tile = next(c for c in result.json["cells"] if c["id"] == loc_id)
        self.assertEqual(tile["state"], "urgent")
        self.assertEqual(tile["tasks"][0]["article"], "NEW_SKU")
        self.assertEqual(tile["last_task"]["completed_qty"], 120)

    def test_manager_only_dashboard_and_api(self):
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["employee_id"] = "anton"
        self.assertEqual(self.client.get("/api/manager/warehouse-map").status_code, 403)
        self.assertEqual(self.client.post("/api/manager/warehouse-map/locations",
                                         json={"labels":["К-444"]}).status_code, 403)
        page = self.client.get("/manager/warehouse-map")
        self.assertEqual(page.status_code, 302)
        self.assertTrue(page.headers["Location"].endswith("/manager-login"))

    def test_bulk_create_preserves_existing_barcode_and_rejects_invalid(self):
        loc_id, label = self.loc()
        original = query_one("SELECT barcode FROM replenishment_locations WHERE id=?", (loc_id,))[0]
        extra = "И-" + str(int(uuid.uuid4().hex[:6],16))
        reply = self.client.post("/api/manager/warehouse-map/locations",
                                 json={"labels":[label.lower(), extra]})
        self.assertEqual(reply.status_code, 201, reply.json)
        self.assertEqual(reply.json["existing"], [label])
        self.assertEqual(reply.json["created"][0]["label"], extra)
        self.assertEqual(query_one("SELECT barcode FROM replenishment_locations WHERE id=?",
                                   (loc_id,))[0], original)
        self.assertIsNotNone(query_one("SELECT barcode FROM replenishment_locations WHERE label=?",
                                       (extra,))[0])
        self.assertEqual(self.client.post("/api/manager/warehouse-map/locations",
                          json={"labels":[extra, extra]}).status_code, 409)
        bad = self.client.post("/api/manager/warehouse-map/locations",
                          json={"labels":["К-123", "<script>"]})
        self.assertEqual(bad.status_code, 400, bad.json)
        self.assertIsNone(query_one("SELECT id FROM replenishment_locations WHERE label='К-123'"))

    def test_map_contains_controls_and_no_false_stock_status(self):
        self.assertIn('id="zones"', WAREHOUSE_MAP_PAGE)
        self.assertIn("Это не карта фактических остатков", WAREHOUSE_MAP_PAGE)
        self.assertIn("fetch('/api/manager/warehouse-map'", WAREHOUSE_MAP_PAGE)
        self.assertIn("interval", WAREHOUSE_MAP_PAGE.lower())


if __name__ == "__main__":
    unittest.main()
