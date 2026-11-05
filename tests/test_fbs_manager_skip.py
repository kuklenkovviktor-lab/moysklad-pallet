"""Supervisor one-use QR authorization and server-persisted FBS pending stage."""
import hashlib
import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "fbs-skip-ui-test-secret")
os.environ.setdefault("WMS_BADGE_SECRET", "fbs-skip-test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "fbs-skip-test-terminal-secret")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "fbs-skip-tests.db")

import app
from fbs_skip_authorization import (
    TOKEN_RE, issue_skip_code, record_pending_item, pending_for_shift,
)
from wms import ensure_db, query_one, execute

class SupervisorSkipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()
        app._WMS_READY.set()

    def setUp(self):
        self.manager=app.app.test_client()
        with self.manager.session_transaction() as s:
            s.clear()
            s["manager_web"]=True
        self.worker=app.app.test_client()
        with self.worker.session_transaction() as s:
            s.clear()
            s["employee_id"]="ilya"
        shift=self.worker.post("/api/fbs/start",json={})
        self.assertEqual(shift.status_code,200,shift.json)
        self.shift_id=shift.json["shift_id"]
        # Previous test may leave an active shift: remove only its pending stage.
        execute("DELETE FROM fbs_pending_products WHERE shift_id=?", (self.shift_id,))

    def test_manager_page_and_one_time_qr(self):
        self.assertEqual(self.manager.get("/manager/fbs-skip").status_code,200)
        view=self.worker.get("/manager/fbs-skip")
        self.assertEqual(view.status_code,302)
        denied=self.worker.post("/api/manager/fbs/skip-qr",json={})
        self.assertEqual(denied.status_code,403)
        qr=self.manager.post("/api/manager/fbs/skip-qr",json={})
        self.assertEqual(qr.status_code,200,qr.json)
        self.assertTrue(qr.json["image"].startswith("data:image/png;base64,"))
        self.assertEqual(qr.headers["Cache-Control"],"no-store")

    def test_item_pending_survives_refresh_and_requires_qr(self):
        record_pending_item(self.shift_id,"ilya","2038719091772","TEST_A","Тестовый товар")
        again=self.worker.get("/api/fbs/pending",query_string={"shift_id":self.shift_id})
        self.assertEqual(again.status_code,200,again.json)
        self.assertEqual(again.json["pending"]["barcode"],"2038719091772")
        with self.assertRaises(ValueError):
            record_pending_item(self.shift_id,"ilya","2038719091773","TEST_B","Другой товар")
        zero=query_one("SELECT COUNT(*) FROM scans")[0]
        code,expires_at=issue_skip_code()
        self.assertRegex(code,TOKEN_RE)
        ok=self.worker.post("/api/fbs/skip-item",json={"shift_id":self.shift_id,"code":code})
        self.assertEqual(ok.status_code,200,ok.json)
        self.assertTrue(ok.json["skipped"])
        self.assertFalse(ok.json["order_counted"])
        self.assertEqual(query_one("SELECT COUNT(*) FROM scans")[0],zero)
        self.assertIsNone(pending_for_shift(self.shift_id,"ilya"))
        event=query_one("SELECT article FROM operation_events WHERE shift_id=? "
                        "AND event_type='fbs_item_skip' ORDER BY created_at DESC LIMIT 1",(self.shift_id,))
        self.assertIsNotNone(event)
        self.assertEqual(event[0],"TEST_A")
        digest=hashlib.sha256(code.encode("ascii")).hexdigest()
        consumed=query_one("SELECT consumed_employee_id,consumed_product_barcode FROM fbs_skip_tokens "
                           "WHERE token_hash=?",(digest,))
        self.assertEqual(consumed[0],"ilya")
        self.assertEqual(consumed[1],"2038719091772")
        record_pending_item(self.shift_id,"ilya","2038719091773","TEST_B","Другой товар")
        reused=self.worker.post("/api/fbs/skip-item",json={"shift_id":self.shift_id,"code":code})
        self.assertEqual(reused.status_code,409)
        self.assertEqual(pending_for_shift(self.shift_id,"ilya")["barcode"],"2038719091773")

    def test_invalid_or_expired_token_leaves_pending(self):
        record_pending_item(self.shift_id,"ilya","2038719091772","TEST_A","Тест")
        bad=self.worker.post("/api/fbs/skip-item",json={"shift_id":self.shift_id,"code":"973"})
        self.assertEqual(bad.status_code,409)
        code,_=issue_skip_code()
        digest=hashlib.sha256(code.encode("ascii")).hexdigest()
        execute("UPDATE fbs_skip_tokens SET expires_at=? WHERE token_hash=?",(time.time()-1,digest))
        expired=self.worker.post("/api/fbs/skip-item",json={"shift_id":self.shift_id,"code":code})
        self.assertEqual(expired.status_code,409)
        self.assertIsNotNone(pending_for_shift(self.shift_id,"ilya"))
        token=query_one("SELECT consumed_at FROM fbs_skip_tokens WHERE token_hash=?",(digest,))
        self.assertIsNone(token[0])

    def test_no_pending_does_not_consume_code(self):
        code,_=issue_skip_code()
        attempt=self.worker.post("/api/fbs/skip-item",json={"shift_id":self.shift_id,"code":code})
        self.assertEqual(attempt.status_code,409)
        digest=hashlib.sha256(code.encode("ascii")).hexdigest()
        self.assertIsNone(query_one("SELECT consumed_at FROM fbs_skip_tokens WHERE token_hash=?",(digest,))[0])

    def test_worker_ui_has_no_reset_button(self):
        html=self.worker.get("/fbs").get_data(as_text=True)
        self.assertNotIn('id="resetProduct"',html)
        self.assertNotIn('clearPendingProduct()',html)
        self.assertIn("acceptSkipQr(code)",html)
        self.assertIn("/api/fbs/pending",html)


if __name__ == "__main__":
    unittest.main()
