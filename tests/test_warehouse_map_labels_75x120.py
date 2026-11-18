"""Synthetic manager-only 75x120 warehouse map printing; existing 58x40 stays intact."""
import base64
import os
import re
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal")
os.environ.setdefault("SECRET_KEY", "synthetic-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "synthetic")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "map-labels.sqlite")

import app
from cell_audit import ensure_cell_audit_db
from warehouse_map_labels import direction_for_location
from wms import execute, init_wms_db, query_one


class WarehouseMapLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        ensure_cell_audit_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as sess:
            sess.clear()
            sess["manager_web"] = True
        self.ids = []
        serial = int(uuid.uuid4().hex[:7], 16)
        for label in (f"К-{serial}", f"К-{serial}-2", f"К-{serial + 1}"):
            loc_id = str(uuid.uuid4())
            code = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
            execute(
                "INSERT INTO replenishment_locations (id,label,barcode,created_at,active)"
                " VALUES (?,?,?,?,1)",
                (loc_id, label, code, "2026-11-18T00:00:00Z"),
            )
            self.ids.append((loc_id, label, code))
        execute("""
            INSERT INTO cell_sku_assignments
            (location_id,product_id,article,product_name,source_barcode,assigned_at,assigned_by)
            VALUES (?,?,?,?,?,?,?)
        """, (self.ids[0][0], "sample-product", "SALAT_3V1",
              "Салатники 3 в 1 ЛАБИРИНТ", "4601234567890",
              "2026-11-18T00:00:00Z", "Руководитель"))

    def test_automatic_arrow_orientation(self):
        self.assertEqual(direction_for_location("К-01"), "down")
        self.assertEqual(direction_for_location("К-01-2"), "up")
        self.assertEqual(direction_for_location("К-01-3"), "up")
        self.assertEqual(direction_for_location("A05-1"), "down")
        self.assertEqual(direction_for_location("A05-2"), "up")

    def test_one_cell_label_is_landscape_75x120_with_permanent_address_codes(self):
        loc_id, label, payload = self.ids[0]
        response = self.client.get("/manager/warehouse-map/labels?id=" + loc_id)
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("@page { size:120mm 75mm; margin:0 }", page)
        self.assertIn("width:120mm;height:75mm", page)
        self.assertIn("Салатники 3 в 1 ЛАБИРИНТ", page)
        self.assertIn('data-arrow="down"', page)
        self.assertEqual(page.count('class="sheet"'), 1)
        qr = re.search(r'<img alt="QR адреса" src="data:image/png;base64,([^"]+)"', page)
        code = re.search(r'<img alt="Code128 адреса" src="data:image/svg\+xml;base64,([^"]+)"', page)
        self.assertIsNotNone(qr)
        self.assertIsNotNone(code)
        self.assertTrue(base64.b64decode(qr.group(1)).startswith(b"\x89PNG"))
        self.assertIn(b"<svg", base64.b64decode(code.group(1)))
        self.assertIn(payload, page)  # printed human-readable address barcode
        self.assertEqual(query_one("SELECT barcode FROM replenishment_locations WHERE id=?",
                                   (loc_id,))[0], payload)

    def test_selected_upper_cell_prints_up_and_no_assumed_stock(self):
        response = self.client.get("/manager/warehouse-map/labels?id=" + self.ids[1][0])
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-arrow="up"', page)
        self.assertIn("ЯЧЕЙКА БЕЗ ЗАКРЕПЛЁННОГО ТОВАРА", page)
        self.assertNotIn("Остаток: 0", page)

    def test_bulk_print_all_registered_cells(self):
        response = self.client.get("/manager/warehouse-map/labels")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(page.count('class="sheet"'), 3)
        self.assertIn('page-break-after:always', page)
        self.assertIn('page-break-after:auto', page)
        self.assertIn("Этикеток:", page)
        self.assertIn("no-store", response.headers.get("Cache-Control", ""))

    def test_badge_user_is_not_manager(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["employee_id"] = "anton"
        self.assertEqual(self.client.get("/manager/warehouse-map/labels").status_code, 302)
        self.assertEqual(self.client.get(
            "/manager/warehouse-map/labels?id=" + self.ids[0][0]).status_code, 302)

    def test_unknown_cell_404_and_map_inline_summary(self):
        self.assertEqual(self.client.get(
            "/manager/warehouse-map/labels?id=unknown").status_code, 404)
        response = self.client.get("/manager/warehouse-map")
        self.assertEqual(response.status_code, 200)
        page = response.get_data(as_text=True)
        self.assertIn("printMapAll", page)
        self.assertIn("cell-brief", page)
        self.assertIn("Не ведётся", page)
        self.assertIn("/manager/warehouse-map/labels?id=", page)

    def test_old_replenishment_labels_remain_58x40(self):
        r = self.client.get("/manager/replenishment/label?id=" + self.ids[0][0])
        self.assertEqual(r.status_code, 200)
        self.assertIn("@page {size:58mm 40mm; margin:0}", r.get_data(as_text=True))
        self.assertNotIn("size:120mm 75mm", r.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
