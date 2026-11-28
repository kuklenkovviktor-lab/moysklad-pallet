"""Synthetic-only tests for WB FBS Sync v1.1. No real network or warehouse DB."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class WBFBSSyncV11Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = patch.dict(os.environ, {
            "DATABASE_URL": "", "WMS_REQUIRE_POSTGRES": "0",
            "WMS_DB_PATH": str(Path(tmp.name) / "synthetic.sqlite"),
            "WMS_BADGE_SECRET": "synthetic-badge-secret",
            "WMS_TERMINAL_SECRET": "synthetic-terminal-secret",
            "WB_API_TOKEN": "",
        })
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location("wms_v11_fixture_" + uuid.uuid4().hex, ROOT / "wms.py")
        wms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wms)
        modules = patch.dict(sys.modules, {"wms": wms})
        modules.start()
        self.addCleanup(modules.stop)
        spec2 = importlib.util.spec_from_file_location("wb_fbs_v11_fixture_" + uuid.uuid4().hex, ROOT / "wb_fbs_sync_v11.py")
        self.mod = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(self.mod)
        self.wms = wms
        self.mod.init_schema()
        module_ref = patch.dict(sys.modules, {"wb_fbs_sync_v11": self.mod})
        module_ref.start()
        self.addCleanup(module_ref.stop)

    def order(self, oid="123", skus=None, chrt="700", status="confirm"):
        return {"id": int(oid), "nmId": 400, "chrtId": int(chrt),
                "article": "CRYSTAL-500", "skus": ["4601234567890"] if skus is None else skus,
                "warehouseId": 21, "status": status}

    def seed(self, account="a", order=None, barcode="WB-QR-123"):
        self.mod.ingest_orders(account, [order or self.order()])
        self.mod.ingest_stickers(account, [{"orderId": 123, "barcode": barcode}])

    def test_product_then_sticker_match(self):
        self.seed()
        value = self.mod.verify_pair("a", "4601234567890", "WB-QR-123")
        self.assertEqual(value["result"], "MATCH")
        self.assertEqual(value["order_id"], "123")

    def test_raw_wb_qr_matches_exact_api_sticker(self):
        self.seed(barcode="*DfgaMEc2")
        verdict = self.mod.verify_pair("a", "4601234567890", "*DfgaMEc2")
        self.assertEqual(verdict["result"], "MATCH")
        self.assertEqual(verdict["order_id"], "123")
        self.assertEqual(self.mod.verify_pair("a", "WRONG", "*DfgaMEc2")["result"], "MISMATCH")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "59997040711")["result"], "UNKNOWN_STICKER")

    def test_russian_hid_qr_matches_api_raw_qr(self):
        self.seed(barcode="*DfgaMEc2")
        raw = "*ВапфЬУс2"
        self.assertEqual(self.mod.normalize_api_sticker(raw), "*DfgaMEc2")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", raw)["result"], "MATCH")
        self.assertEqual(self.mod.verify_pair("a", "WRONG", raw)["result"], "MISMATCH")
        self.assertEqual(self.mod.normalize_api_sticker("4601234567890"), "4601234567890")

    def test_russian_hid_slash_is_restored_and_existing_sticker_matches(self):
        # Exact regression from 2026-11-28 HOSTKEY read-only diagnosis.
        self.seed(barcode="*gDABCD/1")
        raw = "*пВФИСВ.1"
        self.assertEqual(self.mod.normalize_api_sticker(raw), "*gDABCD/1")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", raw)["result"], "MATCH")
        # Fixing the QR must NEVER bypass the independent SKU guard.
        self.assertEqual(self.mod.verify_pair("a", "WRONG", raw)["result"], "MISMATCH")
        self.assertEqual(self.mod.verify_pair("b", "4601234567890", raw)["result"], "UNKNOWN_STICKER")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "*gDABCD/2")["result"], "UNKNOWN_STICKER")

    def test_hid_slash_with_aim_prefix_and_invalid_candidates(self):
        self.seed(barcode="*gDABCD/1")
        self.assertEqual(self.mod.normalize_api_sticker("]Q3;пВФИСВ.1\n"), "*gDABCD/1")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "]Q3;пВФИСВ.1")["result"], "MATCH")
        for bad in ("*пВФИСВ.1EXTRA", "*пВФИСВ:1", "item.123456", "]Q3*bad.!"):
            with self.subTest(bad=bad):
                self.assertEqual(self.mod.normalize_api_sticker(bad), bad)
                self.assertEqual(self.mod.verify_pair("a", "4601234567890", bad)["result"], "UNKNOWN_STICKER")

    def test_scanner_aim_prefix_matches_exact_api_qr(self):
        self.seed(barcode="*DfgaMEc2")
        for suffix in ("0", "3", "9"):
            with self.subTest(aim=suffix):
                raw = "]Q" + suffix + "*DfgaMEc2"
                self.assertEqual(self.mod.normalize_api_sticker(raw), "*DfgaMEc2")
                self.assertEqual(
                    self.mod.verify_pair("a", "4601234567890", raw)["result"], "MATCH"
                )
        self.assertEqual(
            self.mod.verify_pair("a", "WRONG", "]Q3*DfgaMEc2")["result"], "MISMATCH"
        )

    def test_aim_prefix_with_russian_hid_matches_api_qr(self):
        self.seed(barcode="*DfgaMEc2")
        self.assertEqual(
            self.mod.normalize_api_sticker("  ]Q3;ВапфЬУс2\n"), "*DfgaMEc2"
        )
        self.assertEqual(
            self.mod.verify_pair("a", "4601234567890", "]Q3;ВапфЬУс2")["result"], "MATCH"
        )

    def test_invalid_aim_prefix_does_not_fabricate_sticker(self):
        self.seed(barcode="*DfgaMEc2")
        invalid = ("]Q3BAD", "]Q3*bad!", "]Q3*DfgaMEc2junk",
                   "]Qx*DfgaMEc2", "]Q3WB-QR-123", "4601234567890")
        for raw in invalid:
            with self.subTest(raw=raw):
                self.assertEqual(self.mod.normalize_api_sticker(raw), raw)
                self.assertEqual(
                    self.mod.verify_pair("a", "4601234567890", raw)["result"],
                    "UNKNOWN_STICKER",
                )

    def test_confirmed_order_stays_pickable_after_delivery(self):
        self.seed()
        self.assertIsNotNone(self.mod.accepted_product("a", "4601234567890"))
        self.mod.ingest_orders("a", [self.order(status="complete")])
        self.assertEqual(self.wms.query_one(
            "SELECT wb_status FROM wb_fbs_orders WHERE order_id='123'")[0], "complete")
        self.assertIsNotNone(self.mod.accepted_product("a", "4601234567890"))
        self.assertEqual(self.mod.verify_pair(
            "a", "4601234567890", "WB-QR-123")["result"], "MATCH")

    def test_employee_feedback_cancelled_is_explicit_red_error(self):
        self.seed()
        self.mod.ingest_orders("a", [self.order(status="cancel")])
        verdict = self.mod.verify_pair("a", "4601234567890", "WB-QR-123")
        self.assertEqual(verdict["result"], "BLOCKED_STATUS")
        self.assertEqual(verdict["wb_status"], "cancel")
        message, reason = self.wms._wb_scan_feedback(verdict)
        self.assertEqual(message, "ЗАКАЗ ОТМЕНЕН")
        self.assertEqual(reason, "WB_CANCELLED")

    def test_employee_feedback_for_distinct_statuses(self):
        self.seed()
        for status, expectation in [
            ("new", "WB_NOT_ON_ASSEMBLY"),
            ("confirm", "MATCH"),
            ("complete", "MATCH"),
        ]:
            with self.subTest(status=status):
                self.mod.ingest_orders("a", [self.order(status=status)])
                verdict = self.mod.verify_pair("a", "4601234567890", "WB-QR-123")
                self.assertEqual(verdict["wb_status"], status)
                if expectation == "MATCH":
                    self.assertEqual(verdict["result"], "MATCH")
                else:
                    self.assertEqual(self.wms._wb_scan_feedback(verdict)[1], expectation)
        self.assertEqual(self.wms._wb_scan_feedback({"result":"ALREADY_COMPLETE"})[1],"WB_ALREADY_SCANNED")
        self.assertEqual(self.wms._wb_scan_feedback({"result":"MISMATCH"})[1],"WB_PRODUCT_MISMATCH")
        self.assertEqual(self.wms._wb_scan_feedback({"result":"STALE_CACHE"})[1],"WB_STATUS_STALE")
        self.assertEqual(self.wms._wb_scan_feedback({"result":"UNKNOWN_STICKER"})[1],"WB_STICKER_UNKNOWN")
        self.assertEqual(self.wms._wb_scan_feedback({"result":"MAPPING_ERROR"})[1],"WB_PRODUCT_MAPPING_MISSING")

    def test_delivery_without_queue_has_distinct_rejection_reason(self):
        self.seed(order=self.order(status="complete"))
        verdict = self.mod.verify_pair("a","4601234567890","WB-QR-123")
        self.assertEqual(verdict["result"],"BLOCKED_STATUS")
        self.assertEqual(self.wms._wb_scan_feedback(verdict)[1],"WB_DELIVERY_NOT_ADMITTED")

    def test_delivery_import_without_prior_confirmation_is_blocked(self):
        self.seed(order=self.order(status="complete"))
        self.assertIsNone(self.mod.accepted_product("a", "4601234567890"))
        self.assertEqual(self.mod.verify_pair(
            "a", "4601234567890", "WB-QR-123")["result"], "BLOCKED_STATUS")

    def test_cancelled_after_confirm_is_blocked_and_queue_preserved(self):
        self.seed()
        self.mod.ingest_orders("a", [self.order(status="cancel")])
        self.assertEqual(self.wms.query_one(
            "SELECT COUNT(*) FROM wb_fbs_picking_queue")[0], 1)
        self.assertIsNone(self.mod.accepted_product("a", "4601234567890"))
        self.assertEqual(self.mod.verify_pair(
            "a", "4601234567890", "WB-QR-123")["result"], "BLOCKED_STATUS")

    def test_completed_in_grit_stays_blocked_after_delivery(self):
        self.seed()
        self.wms.execute("""UPDATE wb_fbs_orders SET local_status='complete'
            WHERE account_id=? AND order_id=?""", ("a", "123"))
        self.mod.ingest_orders("a", [self.order(status="complete")])
        self.assertIsNone(self.mod.accepted_product("a", "4601234567890"))
        self.assertEqual(self.mod.verify_pair(
            "a", "4601234567890", "WB-QR-123")["result"], "ALREADY_COMPLETE")

    def test_new_orders_add_without_clearing_unassembled_delivery_queue(self):
        self.seed()
        self.mod.ingest_orders("a", [self.order(status="complete")])
        self.mod.ingest_orders("a", [self.order(oid="124",status="confirm")])
        self.assertEqual(self.wms.query_one(
            "SELECT COUNT(*) FROM wb_fbs_picking_queue WHERE account_id='a'")[0], 2)
        self.assertIsNotNone(self.mod.accepted_product("a", "4601234567890"))

    def test_product_only_never_completes(self):
        self.seed()
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "")["result"], "STICKER_REQUIRED")

    def test_sticker_only_never_completes(self):
        self.seed()
        self.assertEqual(self.mod.verify_pair("a", "", "WB-QR-123")["result"], "PRODUCT_REQUIRED")

    def test_wrong_product_blocked(self):
        self.seed()
        self.assertEqual(self.mod.verify_pair("a", "111", "WB-QR-123")["result"], "MISMATCH")

    def test_unknown_sticker_blocked(self):
        self.seed()
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "UNKNOWN")["result"], "UNKNOWN_STICKER")

    def test_variant_mismatch_does_not_use_article_fallback(self):
        self.seed(order=self.order(skus=["9999999999999"], chrt="701"))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MISMATCH")

    def test_missing_skus_fail_closed(self):
        self.seed(order=self.order(skus=[]))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MAPPING_ERROR")

    def test_cancelled_order_blocked(self):
        self.seed(order=self.order(status="cancel"))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "BLOCKED_STATUS")

    def test_ambiguous_sticker_blocked(self):
        self.seed()
        self.mod.ingest_orders("a", [self.order("124")])
        self.mod.ingest_stickers("a", [{"orderId": 124, "barcode": "WB-QR-123"}])
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "AMBIGUOUS_STICKER")

    def test_account_isolation(self):
        self.seed("a")
        self.assertEqual(self.mod.verify_pair("b", "4601234567890", "WB-QR-123")["result"], "UNKNOWN_STICKER")

    def test_order_upsert_keeps_known_skus_when_snapshot_omits_them(self):
        self.seed()
        self.mod.ingest_orders("a", [self.order(skus=[])])
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MATCH")

    def test_sticker_is_updated_without_creating_duplicate_row(self):
        self.seed()
        self.mod.ingest_stickers("a", [{"orderId": 123, "barcode": "NEW-WB-QR"}])
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "UNKNOWN_STICKER")
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "NEW-WB-QR")["result"], "MATCH")

    def test_audit_contains_rejections(self):
        self.seed()
        decision = self.mod.verify_pair("a", "BAD", "WB-QR-123")
        self.mod.log_verification("a", "synthetic-employee", "terminal-1", "BAD", "WB-QR-123", decision)
        result = self.wms.query_one("SELECT result FROM wb_fbs_scan_events")
        self.assertEqual(result[0], "MISMATCH")

    def test_stickers_batch_at_most_100(self):
        class Dummy:
            def __init__(self):
                self.calls = []
            def request(self, method, base, path, token, **kwargs):
                self.calls.append(kwargs["json"]["orders"])
                return {"stickers": []}
        dummy = Dummy()
        client = self.mod.WBClient("synthetic-token")
        client.request = dummy.request
        self.assertEqual(client.stickers(range(205)), [])
        self.assertEqual([len(batch) for batch in dummy.calls], [100, 100, 5])

    def test_network_never_called_by_local_verify(self):
        self.seed()
        with patch.object(self.mod.requests, "get", side_effect=AssertionError("network forbidden")):
            with patch.object(self.mod.requests, "post", side_effect=AssertionError("network forbidden")):
                self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MATCH")

    def test_duplicate_ingest_is_idempotent(self):
        self.seed()
        self.seed()
        self.assertEqual(self.wms.query_one("SELECT count(*) FROM wb_fbs_orders")[0], 1)
        self.assertEqual(self.wms.query_one("SELECT count(*) FROM wb_fbs_stickers")[0], 1)


    def test_catalog_fallback_exact_nm_and_chrt(self):
        self.mod.ingest_catalog("a", [{
            "nmID": 400,
            "sizes": [
                {"chrtID": 700, "skus": ["4601234567890"]},
                {"chrtID": 701, "skus": ["9999999999999"]},
            ]}])
        self.seed(order=self.order(skus=[]))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MATCH")
        self.assertEqual(self.mod.verify_pair("a", "9999999999999", "WB-QR-123")["result"], "MISMATCH")

    def test_catalog_missing_variant_never_falls_back_to_nm_only(self):
        self.mod.ingest_catalog("a", [{"nmID":400,"sizes":[{"chrtID":701,"skus":["4601234567890"]}]}])
        self.seed(order=self.order(skus=[],chrt="700"))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "MAPPING_ERROR")

    def test_local_state_complete_prevents_repeat(self):
        self.seed()
        self.wms.execute("""UPDATE wb_fbs_orders SET local_status='complete'
            WHERE account_id=? AND order_id=?""", ("a","123"))
        self.assertEqual(self.mod.verify_pair("a", "4601234567890", "WB-QR-123")["result"], "ALREADY_COMPLETE")

    def test_existing_fbs_route_rejects_wrong_product_then_accepts_new_pair(self):
        from flask import Flask
        from fbs_skip_authorization import record_pending_item
        self.wms.init_wms_db()
        emp = self.wms.query_one("SELECT id FROM employees WHERE active=1 AND role!='manager' LIMIT 1")[0]
        shift = str(uuid.uuid4())
        stamp = self.wms.now_iso()
        self.wms.execute("INSERT INTO shifts (id,employee_id,started_at) VALUES(?,?,?)", (shift,emp,stamp))
        self.wms.execute("""INSERT INTO activities
            (id,shift_id,employee_id,activity_type,started_at) VALUES(?,?,?,?,?)""",
            (str(uuid.uuid4()),shift,emp,"picking",stamp))
        app = Flask(__name__)
        app.secret_key = "synthetic-session"
        app.testing = True
        app.register_blueprint(self.wms.wms_bp)
        client = app.test_client()
        with client.session_transaction() as ss:
            ss["employee_id"] = emp
            ss["employee_shift_id"] = shift
        self.seed(barcode="*DfgaMEc2")
        with patch.dict(os.environ, {"WB_FBS_V11_ENABLED":"1","WB_FBS_V11_ACCOUNT_ID":"a"}):
            record_pending_item(shift,emp,"WRONG","test","test")
            response=client.post("/api/fbs/scan",json={
                "shift_id":shift,"barcode":"*ВапфЬУс2","product_barcode":"WRONG",
                "request_id":str(uuid.uuid4())})
            self.assertEqual(response.status_code,409,response.json)
            self.assertEqual(response.json["reason"],"WB_PRODUCT_MISMATCH")
            self.assertTrue(response.json["reset_pending"])
            self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM scans")[0],0)
            record_pending_item(shift,emp,"4601234567890","test","test")
            payload={"shift_id":shift,"barcode":"*ВапфЬУс2",
                     "product_barcode":"4601234567890","request_id":str(uuid.uuid4())}
            ok=client.post("/api/fbs/scan",json=payload)
            self.assertEqual(ok.status_code,200,ok.json)
            self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM scans")[0],1)
            self.assertEqual(self.wms.query_one(
                "SELECT local_status FROM wb_fbs_orders WHERE order_id='123'")[0],"complete")
            retry=client.post("/api/fbs/scan",json=payload)
            self.assertEqual(retry.status_code,200,retry.json)
            self.assertTrue(retry.json["replayed"])
            self.assertEqual(self.wms.query_one("SELECT COUNT(*) FROM scans")[0],1)

    def test_fbs_scan_route_accepts_aim_prefixed_api_qr(self):
        from flask import Flask
        from fbs_skip_authorization import record_pending_item
        self.wms.init_wms_db()
        emp = self.wms.query_one(
            "SELECT id FROM employees WHERE active=1 AND role!='manager' LIMIT 1"
        )[0]
        shift = str(uuid.uuid4())
        stamp = self.wms.now_iso()
        self.wms.execute(
            "INSERT INTO shifts (id,employee_id,started_at) VALUES(?,?,?)",
            (shift, emp, stamp),
        )
        self.wms.execute(
            """INSERT INTO activities
            (id,shift_id,employee_id,activity_type,started_at)
            VALUES(?,?,?,?,?)""",
            (str(uuid.uuid4()), shift, emp, "picking", stamp),
        )
        app = __import__("flask").Flask(__name__)
        app.secret_key = "synthetic-session"
        app.testing = True
        app.register_blueprint(self.wms.wms_bp)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["employee_id"] = emp
            sess["employee_shift_id"] = shift
        self.seed(barcode="*DfgaMEc2")
        with patch.dict(os.environ, {
            "WB_FBS_V11_ENABLED": "1", "WB_FBS_V11_ACCOUNT_ID": "a"
        }):
            record_pending_item(shift, emp, "4601234567890", "test", "test")
            response = client.post("/api/fbs/scan", json={
                "shift_id": shift,
                "barcode": "]Q3*DfgaMEc2",
                "product_barcode": "4601234567890",
                "request_id": str(uuid.uuid4()),
            })
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(self.wms.query_one(
                "SELECT COUNT(*) FROM scans"
            )[0], 1)
            self.assertEqual(self.wms.query_one(
                "SELECT local_status FROM wb_fbs_orders WHERE order_id='123'"
            )[0], "complete")

    def test_statuses_reject_failed_auth_without_retry(self):
        class Fake:
            status_code=401
            headers={}
            def raise_for_status(self):
                raise self.mod.requests.HTTPError("401")
        # Auth error raised before any retry (mock configured without network).
        client = self.mod.WBClient("synthetic")
        fake = type("Response", (), {"status_code":401, "raise_for_status": lambda s: (_ for _ in ()).throw(self.mod.requests.HTTPError("401"))})()
        client.http = type("Http", (), {"request":lambda s,*args,**kwargs: fake})()
        with self.assertRaises(self.mod.requests.HTTPError):
            client.orders()


if __name__ == "__main__":
    unittest.main()
