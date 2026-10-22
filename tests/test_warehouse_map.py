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
