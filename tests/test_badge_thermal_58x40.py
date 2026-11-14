"""59x40? No: exact 58x40 thermal badge labels. Synthetic SQLite only."""
import importlib.util
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

ROOT=Path(__file__).resolve().parents[1]

class ThermalBadgeTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env=patch.dict(os.environ,{"WMS_BADGE_SECRET":"synthetic-badge-key",
                                    "WMS_TERMINAL_SECRET":"synthetic-terminal-key",
                                    "DATABASE_URL":"","WMS_REQUIRE_POSTGRES":"0",
                                    "WMS_DB_PATH":str(Path(tmp.name)/"fixture.db")})
        env.start();self.addCleanup(env.stop)
        spec=importlib.util.spec_from_file_location("thermal_badge_"+uuid.uuid4().hex,ROOT/"wms.py")
        self.wms=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.wms)
        modules=patch.dict(sys.modules,{"wms":self.wms});modules.start();self.addCleanup(modules.stop)
        self.wms.init_wms_db()
        app=Flask(__name__);app.secret_key="test-synthetic-secret"
        app.register_blueprint(self.wms.wms_bp)
        self.client=app.test_client()
        with self.client.session_transaction() as sess:sess["manager_web"]=True

    def test_one_label_58x40_same_qr_after_rename(self):
        before=self.wms.query_one("SELECT badge_code FROM employees WHERE id='loader'")[0]
        changed=self.client.patch("/api/admin/badges/loader",json={"name":"Християн"})
        self.assertEqual(changed.status_code,200,changed.json)
        self.assertEqual(changed.json["name"],"Християн")
        self.assertEqual(self.wms.query_one("SELECT name,badge_code FROM employees WHERE id='loader'"),
                         ("Християн",before))
        updated=self.client.get("/api/admin/badges")
        self.assertEqual(updated.status_code,200,updated.json)
        self.assertIn("Християн",[row["name"] for row in updated.json["employees"]])
        page=self.client.get("/manager/badges/label?id=loader")
        self.assertEqual(page.status_code,200)
        html=page.get_data(as_text=True)
        self.assertIn("@page {size:58mm 40mm; margin:0}",html)
        self.assertIn("width:58mm;height:40mm",html)
        self.assertIn("width:30mm;height:30mm",html)
        self.assertIn("Християн",html)
        self.assertIn("№4",html)
        self.assertIn("image/png;base64,",html)
        self.assertIn("window.print()",html)
        self.assertNotIn(before,html)
        self.assertNotIn("grit-ui.css",html)
        self.assertIn("Cache-Control",page.headers)

    def test_bulk_print_one_page_per_active_employee(self):
        page=self.client.get("/manager/badges/labels/all")
        self.assertEqual(page.status_code,200)
        html=page.get_data(as_text=True)
        self.assertEqual(html.count('class="sheet"'),5)
        self.assertIn("break-after:page",html)
        self.assertIn("page-break-after:always",html)
        self.assertIn('@page {size:58mm 40mm; margin:0}',html)
        self.assertNotIn('data-action="archive"',html)

    def test_archived_badge_excluded_and_cannot_print_individually(self):
        self.wms.execute("UPDATE employees SET active=0 WHERE id='loader'")
        page=self.client.get("/manager/badges/label?id=loader")
        self.assertEqual(page.status_code,404)
        all_page=self.client.get("/manager/badges/labels/all")
        self.assertEqual(all_page.get_data(as_text=True).count('class="sheet"'),4)

    def test_no_manager_access_to_print(self):
        with self.client.session_transaction() as sess:sess.clear()
        self.assertEqual(self.client.get("/manager/badges/label?id=loader").status_code,302)
        self.assertEqual(self.client.get("/manager/badges/labels/all").status_code,302)

    def test_escaping_long_name_without_payload_changes(self):
        value="<Тест & Проверка>"
        self.wms.execute("UPDATE employees SET name=? WHERE id='loader'",(value,))
        page=self.client.get("/manager/badges/label?id=loader")
        html=page.get_data(as_text=True)
        self.assertIn("&lt;Тест &amp; Проверка&gt;",html)
        self.assertNotIn("<Тест & Проверка>",html)

    def test_badge_ui_uses_thermal_routes_not_full_page_print(self):
        source=(ROOT/"wms.py").read_text(encoding="utf-8")
        html=source.split('BADGES_PAGE = """',1)[1].split('"""',1)[0]
        self.assertIn("/manager/badges/labels/all",html)
        self.assertIn("/manager/badges/label?id=",html)
        self.assertNotIn("function printAllBadges(){clearPrintSelection();window.print()}",html)

if __name__=="__main__":
    unittest.main()
