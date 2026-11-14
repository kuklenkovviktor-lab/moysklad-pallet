"""Synthetic Google Sheets API tests: no live network, credentials or warehouse."""
import os
import unittest
from unittest.mock import patch
from flask import Flask
from ozon_fbo_dimensions import read_dimensions_values
from ozon_fbo_route_lab import route_lab_bp
from grit_google_sheets import GoogleSheetsSyncError, sync_configuration, _CACHE, get_google_catalog


DIM=[["Артикул","Длина ","Ширина ","Высота","Вес"],["TEST-XL","50","40","35","4,2"],
     ["TEST-S","8","8","22","0,5"],["TEST-NO", "", "", "", "12"]]
SUMMARY=[["TEST-XL","2000000000001"],["TEST-S","2000000000002"]]


class GoogleSyncTests(unittest.TestCase):
    def setUp(self):
        _CACHE.update(catalog=None,until=0,synced_at=None,spreadsheet_id=None)

    def test_google_values_parse_dimensions_and_barcodes(self):
        c=read_dimensions_values(DIM,SUMMARY)
        self.assertEqual(len(c["items"]),2)
        self.assertEqual(c["items"]["test-xl"]["weight_kg"],4.2)
        self.assertEqual(c["barcodes"]["test-s"],["2000000000002"])
        self.assertEqual(len(c["incomplete"]),1)

    def test_incorrect_header_rejected(self):
        with self.assertRaises(ValueError):
            read_dimensions_values([["SKU","L","W","H","K"]],[])

    def test_no_server_credentials_is_fail_closed(self):
        with patch.dict(os.environ,{"GRIT_GOOGLE_SERVICE_ACCOUNT_JSON":"",
                                    "GRIT_GOOGLE_SPREADSHEET_ID":""}):
            self.assertFalse(sync_configuration()["configured"])
            with self.assertRaises(GoogleSheetsSyncError):
                get_google_catalog()

    def test_refresh_and_cache_use_mocked_google_only(self):
        with patch.dict(os.environ,{"GRIT_GOOGLE_SPREADSHEET_ID":"FAKE_SPREADSHEET_123",
                                    "GRIT_GOOGLE_SERVICE_ACCOUNT_JSON":
                                    '{"type":"service_account","client_email":"synthetic@test.invalid","private_key":"synthetic"}'}), \
             patch("grit_google_sheets._google_values",return_value=(DIM,SUMMARY)) as fetch:
            first, t, hit=get_google_catalog()
            self.assertFalse(hit)
            second, _, hit=get_google_catalog()
            self.assertTrue(hit)
            self.assertEqual(first,second)
            self.assertEqual(fetch.call_count,1)
            get_google_catalog(force=True)
            self.assertEqual(fetch.call_count,2)

    def test_manager_only_sync_endpoint_and_preview(self):
        app=Flask(__name__);app.secret_key="synthetic-route-lab"
        app.testing=True;app.register_blueprint(route_lab_bp)
        client=app.test_client()
        self.assertEqual(client.get("/api/manager/ozon-fbo/route-lab/google-sync").status_code,403)
        with client.session_transaction() as session:
            session["manager_web"]=True
        fake_catalog=read_dimensions_values(DIM,SUMMARY)
        with patch("ozon_fbo_route_lab.get_google_catalog",
                   return_value=(fake_catalog,"2026-11-14T10:00:00+00:00",True)), \
             patch("ozon_fbo_route_lab._map_assignments_for_preview",
                   return_value=[("TEST-XL","А-04"),("TEST-S","Ж-12")]) as read_map:
            status=client.get("/api/manager/ozon-fbo/route-lab/google-sync")
            self.assertEqual(status.status_code,200)
            self.assertEqual(status.json["catalog_complete"],2)
            route=client.post("/api/manager/ozon-fbo/route-lab/preview",
                              data={"basket":"TEST-XL;2\nTEST-S;1","mode":"hybrid"})
        self.assertEqual(route.status_code,200,route.json)
        self.assertEqual(route.json["matched"],2)
        self.assertTrue(route.json["synthetic_basket"])
        self.assertFalse(route.json["saved"])
        read_map.assert_called_once()

    def test_google_errors_are_safe_for_manager(self):
        app=Flask(__name__);app.secret_key="synthetic-route-lab"
        app.testing=True;app.register_blueprint(route_lab_bp)
        client=app.test_client()
        with client.session_transaction() as session:
            session["manager_web"]=True
        with patch("ozon_fbo_route_lab.get_google_catalog",
                   side_effect=GoogleSheetsSyncError("Google Sheets отказал в доступе")):
            r=client.get("/api/manager/ozon-fbo/route-lab/google-sync")
        self.assertEqual(r.status_code,503)
        self.assertNotIn("private_key",r.get_data(as_text=True))


if __name__=="__main__":
    unittest.main()
