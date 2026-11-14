"""Synthetic badge-management regression, using a temporary SQLite database only."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from flask import Flask

ROOT=Path(__file__).resolve().parents[1]

class BadgeManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env=patch.dict(os.environ,{
            "WMS_BADGE_SECRET":"synthetic-badge-secret",
            "WMS_TERMINAL_SECRET":"synthetic-terminal-secret",
            "DATABASE_URL":"",
            "WMS_REQUIRE_POSTGRES":"0",
            "WMS_DB_PATH":str(Path(self.tmp.name)/"test.db"),
        })
        env.start();self.addCleanup(env.stop)
        spec=importlib.util.spec_from_file_location("wms_badge_fixture_"+uuid.uuid4().hex, ROOT/"wms.py")
        self.wms=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.wms)
        modules=patch.dict(sys.modules,{"wms":self.wms})
        modules.start();self.addCleanup(modules.stop)
        self.wms.init_wms_db()
        self.app=Flask(__name__)
        self.app.secret_key="synthetic-manager-cookie"
        self.app.register_blueprint(self.wms.wms_bp)
        self.client=self.app.test_client()
        with self.client.session_transaction() as ses:
            ses["manager_web"]=True

    def create(self,name="Новый сборщик"):
        return self.client.post("/api/admin/badges",json={"name":name})

    def test_add_and_qr_stays_valid_after_rename(self):
        created=self.create()
        self.assertEqual(created.status_code,201,created.json)
        eid=created.json["id"]
        self.assertEqual(created.json["badge_number"],6)
        original=self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(eid,))[0]
        renamed=self.client.patch("/api/admin/badges/"+eid,json={"name":"Сборщик Пётр"})
        self.assertEqual(renamed.status_code,200,renamed.json)
        details=self.wms.query_one("SELECT name,badge_code,active FROM employees WHERE id=?",(eid,))
        self.assertEqual(details,("Сборщик Пётр",original,1))
        login=self.app.test_client().post("/api/auth/login",json={"badge_code":original})
        self.assertEqual(login.status_code,200,login.json)
        self.assertEqual(login.json["employee"]["name"],"Сборщик Пётр")
        badges=self.client.get("/api/admin/badges")
        self.assertEqual(badges.status_code,200)
        self.assertTrue(any(x["id"]==eid for x in badges.json["employees"]))

    def test_archive_retains_shift_history_and_blocks_old_credentials(self):
        created=self.create("Сотрудник для архива")
        self.assertEqual(created.status_code,201,created.json)
        eid=created.json["id"]
        original=self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(eid,))[0]
        shift=str(uuid.uuid4())
        self.wms.execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,?)",
                         (shift,eid,"2026-11-14T08:00:00+00:00","2026-11-14T09:00:00+00:00"))
        archived=self.client.post("/api/admin/badges/"+eid+"/archive",json={})
        self.assertEqual(archived.status_code,200,archived.json)
        self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM shifts WHERE employee_id=?",(eid,))[0],1)
        self.assertEqual(self.app.test_client().post("/api/auth/login",json={"badge_code":original}).status_code,404)
        self.assertTrue(any(x["id"]==eid for x in self.client.get("/api/admin/badges").json["archived"]))
        # An init/deploy does not resurrect an archived account.
        self.wms.init_wms_db()
        self.assertEqual(self.wms.query_one("SELECT active FROM employees WHERE id=?",(eid,))[0],0)
        new=self.create("Следующий сотрудник")
        self.assertEqual(new.status_code,201,new.json)
        self.assertGreater(new.json["badge_number"],created.json["badge_number"])
        restored=self.client.post("/api/admin/badges/"+eid+"/restore",json={})
        self.assertEqual(restored.status_code,200,restored.json)
        self.assertEqual(self.wms.query_one("SELECT active FROM employees WHERE id=?",(eid,))[0],1)

    def test_open_shift_blocks_archive_and_manager_protected(self):
        eid=self.create("Сотрудник в смене").json["id"]
        shift=str(uuid.uuid4())
        self.wms.execute("INSERT INTO shifts(id,employee_id,started_at,ended_at) VALUES(?,?,?,NULL)",
                         (shift,eid,"2026-11-14T08:00:00+00:00"))
        res=self.client.post("/api/admin/badges/"+eid+"/archive",json={})
        self.assertEqual(res.status_code,409,res.json)
        self.assertEqual(self.wms.query_one("SELECT active FROM employees WHERE id=?",(eid,))[0],1)
        self.assertEqual(self.client.post("/api/admin/badges/viktor/archive",json={}).status_code,403)

    def test_permissions_and_duplicate_names(self):
        created=self.create("Тестовое имя")
        self.assertEqual(created.status_code,201)
        self.assertEqual(self.create("ТЕСТОВОЕ ИМЯ").status_code,409)
        self.assertEqual(self.create(" ").status_code,400)
        self.assertEqual(self.create("A"+chr(10)+"B").status_code,400)
        with self.app.test_client() as stranger:
            self.assertEqual(stranger.post("/api/admin/badges",json={"name":"Неавторизованный"}).status_code,403)
            self.assertEqual(stranger.patch("/api/admin/badges/"+created.json["id"],json={"name":"Чужой"}).status_code,403)
            self.assertEqual(stranger.post("/api/admin/badges/"+created.json["id"]+"/archive",json={}).status_code,403)
        # Existing identities are stable; no QR rotation for label changes.
        qr_before=self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(created.json["id"],))[0]
        self.assertEqual(self.client.patch("/api/admin/badges/"+created.json["id"],json={"name":"Новое имя"}).status_code,200)
        self.assertEqual(self.wms.query_one("SELECT badge_code FROM employees WHERE id=?",(created.json["id"],))[0],qr_before)

if __name__=="__main__":
    unittest.main()
