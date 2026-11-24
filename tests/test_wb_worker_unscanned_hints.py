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

    def test_printed_sticker_numbers_article_and_no_mutation(self):
        fake = [("101", "6008122", "9340", "1022_MIX", "2026-11-24T08:00:00+03:00"),
                ("102", "6008123", "6471", "1022_MIX", "2026-11-24T09:00:00+03:00")]
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ,
                {"WB_FBS_V11_ENABLED": "1", "WB_FBS_V11_ACCOUNT_ID": "default"}), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "_wb_hint_today", return_value="2026-11-24"), \
                patch.object(wms, "query_all", return_value=fake) as query:
            res = self.get()
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.json["remaining"], 2)
            self.assertEqual(res.json["day"], "2026-11-24")
            self.assertEqual(res.json["stickers"], [
                {"part_a": "6008122", "part_b": "9340"},
                {"part_a": "6008123", "part_b": "6471"}])
            self.assertEqual(res.json["article"], "1022_MIX")
            self.assertTrue(res.json["shared"])
            self.assertNotIn("101", str(res.json))
            self.assertEqual(query.call_count, 1)
            sql, args = query.call_args.args
            self.assertIn("t.part_a,t.part_b,o.seller_article", sql)
            self.assertIn("o.local_status='pending'", sql)
            self.assertIn("o.wb_status IN ('confirm','complete')", sql)
            self.assertEqual(args, ("default", "460111"))

    def test_missing_printed_numbers_not_faked_from_qr(self):
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ,
                {"WB_FBS_V11_ENABLED": "1"}), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "_wb_hint_today", return_value="2026-11-24"), \
                patch.object(wms, "query_all", return_value=[
                    ("101", None, None, "1022_MIX", "2026-11-24T08:00:00+03:00"),
                    ("102", "6008122", "zhyU", "1022_MIX", "2026-11-24T09:00:00+03:00")]):
            result = self.get().json
            self.assertEqual(result["remaining"], 2)
            self.assertEqual(result["stickers"], [])
            self.assertNotIn("sticker_suffixes", result)

    def test_only_today_moscow_ignores_previous_days_and_unknown(self):
        rows = [
            ("old22", "6008122", "1111", "ART", "2026-11-22T12:00:00+03:00"),
            ("old23", "6008122", "2222", "ART", "2026-11-23T12:00:00+03:00"),
            ("utc23", "6008122", "3333", "ART", "2026-11-23T21:30:00+00:00"),
            ("unknown", "6008122", "4444", "ART", None),
            ("today", "6008122", "9340", "ART", "2026-11-24T18:00:00+03:00"),
        ]
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ,
                {"WB_FBS_V11_ENABLED": "1"}), \
                patch.object(wms, "_wb_hint_today", return_value="2026-11-24"), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "query_all", return_value=rows):
            data = self.get().json
            self.assertEqual(data["day"], "2026-11-24")
            self.assertEqual(data["remaining"], 2)
            self.assertEqual([s["part_b"] for s in data["stickers"]], ["3333", "9340"])
            self.assertNotIn("1111", str(data))
            self.assertNotIn("2222", str(data))
            self.assertNotIn("4444", str(data))

    def test_more_than_100_old_rows_do_not_hide_today(self):
        rows = [(str(i), "6008122", "1111", "ART", "2026-11-22T12:00:00+03:00")
                for i in range(120)]
        rows.append(("today", "6008122", "9340", "ART", "2026-11-24T09:00:00+03:00"))
        with patch.object(wms, "ensure_db"), patch.object(wms, "get_session_employee",
                return_value={"id": "staff1"}), patch.dict(os.environ,
                {"WB_FBS_V11_ENABLED": "1"}), \
                patch.object(wms, "_wb_hint_today", return_value="2026-11-24"), \
                patch.object(wms, "query_one", return_value=("staff1", None)), \
                patch.object(wms, "query_all", return_value=rows):
            data = self.get().json
            self.assertEqual(data["remaining"], 1)
            self.assertFalse(data["truncated"])
            self.assertEqual(data["stickers"][0]["part_b"], "9340")

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
        self.assertIn("x.part_a+' / '+x.part_b",ui)
        self.assertIn("Осталось от предыдущего товара сегодня",ui)


if __name__ == "__main__":
    unittest.main()
