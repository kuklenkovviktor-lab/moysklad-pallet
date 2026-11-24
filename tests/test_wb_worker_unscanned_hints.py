"""Small synthetic tests for read-only worker WB remainder hints."""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault("WMS_BADGE_SECRET", "synthetic-badge-only")
os.environ.setdefault("WMS_TERMINAL_SECRET", "synthetic-terminal-only")
os.environ["DATABASE_URL"] = ""
os.environ["WMS_REQUIRE_POSTGRES"] = "0"

import wms
from flask import Flask


class WorkerWbHintTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(wms.wms_bp)
        self.client = self.app.test_client()

    def get(self, barcode="460111", shift_id="shift1"):
        return self.client.get("/api/fbs/wb-unscanned-hints",
                               query_string={"shift_id": shift_id, "barcode": barcode})

    def test_requires_badge(self):
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee", return_value=None):
            self.assertEqual(self.get().status_code, 401)

    def test_requires_own_active_shift(self):
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ, {"WB_FBS_V11_ENABLED": "1"}), \
                patch.object(wms, "query_one", return_value=("another", None)):
            self.assertEqual(self.get().status_code, 403)

    def test_disabled_has_no_fake_zero(self):
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ, {"WB_FBS_V11_ENABLED": "0"}):
            result = self.get().json
            self.assertFalse(result["available"])
            self.assertNotIn("remaining", result)

    def test_shared_queue_suffixes_only_and_no_mutation(self):
        fake = [("101", "WB-ABCD6471", "1022_MIX"), ("102", "WB-ABC34286", "1022_MIX")]
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ,
                {"WB_FBS_V11_ENABLED": "1", "WB_FBS_V11_ACCOUNT_ID": "default"}), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "query_all", return_value=fake) as query:
            res = self.get()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json["remaining"], 2)
            self.assertEqual(res.json["sticker_suffixes"], ["6471", "4286"])
            self.assertTrue(res.json["shared"])
            self.assertNotIn("101", str(res.json))
            self.assertEqual(query.call_count, 1)
            sql, args = query.call_args.args
            self.assertIn("o.local_status='pending'", sql)
            self.assertIn("o.wb_status IN ('confirm','complete')", sql)
            self.assertEqual(args, ("default", "460111"))

    def test_no_matches_is_valid_zero_when_enabled(self):
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ, {"WB_FBS_V11_ENABLED": "1"}), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "query_all", return_value=[]):
            self.assertEqual(self.get().json["remaining"], 0)

    def test_fbs_worker_ui_is_nonblocking(self):
        from pathlib import Path
        source = Path("wms.py").read_text(encoding="utf-8")
        ui = source.split('FBS_PAGE = """',1)[1].split('"""',1)[0]
        self.assertIn('id="wbUnscannedHint"',ui)
        self.assertIn('id="wbPreviousHint"',ui)
        self.assertIn('refreshWbUnscannedHint(previous,true)',ui)
        self.assertIn('setTimeout(()=>refreshWbUnscannedHint(d.barcode),0)',ui)
        self.assertIn("async function handleScan(code)",ui)


if __name__ == "__main__":
    unittest.main()
