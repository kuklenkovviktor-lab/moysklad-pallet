"""Synthetic-only regression for WB v1.1 missing-sticker cache repair.

No real WB token, no network, no production DB, no changed scan admission.
"""
import unittest
from unittest.mock import Mock, patch

import wb_fbs_sync_v11 as wb

QR="*Dc5xlawC"


class StickerRepairTests(unittest.TestCase):
    def setUp(self):
        self.source=patch.object(wb,"init_schema")
        self.query=patch("wms.query_all")
        self.execute=patch("wms.execute")
        self.ingest=patch.object(wb,"ingest_stickers",return_value=1)
        self.source.start()
        self.rows=self.query.start()
        self.writes=self.execute.start()
        self.saved=self.ingest.start()
        self.addCleanup(self.source.stop)
        self.addCleanup(self.query.stop)
        self.addCleanup(self.execute.stop)
        self.addCleanup(self.ingest.stop)
        self.client=Mock()
        self.client.stickers.return_value=[
            {"orderId":1001,"barcode":QR,"partA":"123","partB":"4567"}
        ]

    def test_repairs_sticker_for_already_existing_active_order(self):
        self.rows.return_value=[("1001",)]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result,{"missing_checked":1,"stickers_received":1})
        self.client.stickers.assert_called_once_with([1001])
        self.saved.assert_called_once()
        self.assertEqual(self.saved.call_args.args[0],"acct-demo")
        self.assertEqual(self.saved.call_args.args[1][0]["barcode"],QR)
        query,args=self.rows.call_args.args
        self.assertIn("wb_fbs_picking_queue",query)
        self.assertIn("s.order_id IS NULL",query)
        self.assertIn("o.local_status='pending'",query)
        self.assertIn("LIMIT 100",query)
        self.assertIn("CASE WHEN r.attempted_at IS NULL",query)
        self.assertEqual(args[0],"acct-demo")
        self.writes.assert_called_once()

    def test_empty_missing_queue_never_contacts_wb(self):
        self.rows.return_value=[]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result["missing_checked"],0)
        self.client.stickers.assert_not_called()
        self.writes.assert_not_called()

    def test_bad_qr_from_wb_is_not_saved_or_fabricated(self):
        self.rows.return_value=[("1001",)]
        self.client.stickers.return_value=[
            {"orderId":1001,"barcode":"60227855344"},
            {"orderId":1001,"barcode":"broken"},
        ]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result["stickers_received"],0)
        self.saved.assert_not_called()
        self.writes.assert_called_once()

    def test_other_order_sticker_never_leaks_into_account(self):
        self.rows.return_value=[("1001",)]
        self.client.stickers.return_value=[
            {"orderId":2002,"barcode":QR}
        ]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result["stickers_received"],0)
        self.saved.assert_not_called()

    def test_wb_timeout_is_not_a_fake_success(self):
        self.rows.return_value=[("1001",)]
        self.client.stickers.side_effect=TimeoutError("synthetic offline timeout")
        with self.assertRaises(TimeoutError):
            wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.writes.assert_called_once()  # retry checkpoint set before network
        self.saved.assert_not_called()

    def test_no_more_than_100_retry_ids(self):
        self.rows.return_value=[(str(i),) for i in range(1,101)]
        self.client.stickers.return_value=[]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result["missing_checked"],100)
        self.assertEqual(len(self.client.stickers.call_args.args[0]),100)
        self.assertEqual(self.writes.call_count,100)

    def test_non_numeric_order_ids_are_not_guessed(self):
        self.rows.return_value=[("not-an-order",),("00101",)]
        result=wb._sync_missing_stickers_once_unlocked("acct-demo",self.client)
        self.assertEqual(result["stickers_received"],0)
        self.client.stickers.assert_not_called()
        self.writes.assert_not_called()

    def test_missing_sticker_worker_is_scheduled(self):
        stop=Mock()
        stop.is_set.return_value=False
        stop.wait.return_value=True
        client=Mock()
        with patch.object(wb,"_sync_new_once_unlocked"), \
             patch.object(wb,"_sync_status_once_unlocked"), \
             patch.object(wb,"_sync_missing_stickers_once_unlocked") as repair, \
             patch.object(wb,"_sync_catalog_once_unlocked"), \
             patch.object(wb,"sync_once"), \
             patch.object(wb,"_exclusive_sync"):
            wb.worker_loop("acct-demo",client,stop)
        repair.assert_called_once_with("acct-demo",client)

    def test_legacy_qr_normalization_preserved(self):
        self.assertEqual(wb.normalize_api_sticker(QR),QR)
        self.assertEqual(wb.normalize_api_sticker("]Q3"+QR),QR)
        self.assertEqual(wb.normalize_api_sticker(";"+QR[1:]),QR)
        self.assertEqual(wb.normalize_api_sticker("60227855344"),"60227855344")


class ManagerStickerDiagnosisTests(unittest.TestCase):
    def setUp(self):
        from flask import Flask
        self.app=Flask("synthetic-wb-sticker-diagnostics")
        self.app.secret_key="synthetic-session-secret"

    def diagnose(self, *, own=0, pdf=0,
                 sticker="*Dc5xlawC", allowed=True, authorized=True):
        from flask import session
        def q(sql,params=()):
            if "FROM wb_fbs_stickers" in sql:
                return (own,)
            if "FROM pdf_fbs_orders" in sql:
                return (1,) if pdf else None
            raise AssertionError("Unexpected diagnostic SQL")
        with self.app.test_request_context(
            "/api/wb-v11/diagnose-sticker",method="POST",
            headers={"X-CSRF-Token":"test-csrf"},
            json={"sticker":sticker}):
            session["wb_fbs_v11_csrf"]="test-csrf"
            with patch.object(wb,"manager_access",return_value=authorized), \
                 patch.object(wb,"_allowed",return_value=allowed), \
                 patch.object(wb,"init_schema"), \
                 patch.object(wb,"_account",return_value="acct-demo"), \
                 patch("wms.query_one",side_effect=q):
                result=wb.manager_wb_diagnose_sticker_v11()
            return result if isinstance(result,tuple) else (result,200)

    def test_known_current_account_qr(self):
        response,status=self.diagnose(own=1)
        self.assertEqual(status,200)
        self.assertEqual(response.json["reason"],"IN_SELECTED_ACCOUNT")
        self.assertEqual(response.headers["Cache-Control"],"no-store")

    def test_unknown_qr_is_never_looked_up_across_other_accounts(self):
        response,status=self.diagnose()
        self.assertEqual(status,200)
        self.assertEqual(response.json["reason"],"NOT_IN_WB_API_CACHE")
        self.assertNotIn("other_account_has_match",response.json)

    def test_qr_in_pdf_only_is_not_accepted_as_wb_api(self):
        response,status=self.diagnose(pdf=1)
        self.assertEqual(status,200)
        self.assertEqual(response.json["reason"],"PDF_REGISTRY_ONLY")
        self.assertFalse(response.json["account_has_match"])

    def test_missing_valid_qr_is_explicitly_diagnosed(self):
        response,status=self.diagnose()
        self.assertEqual(status,200)
        self.assertEqual(response.json["reason"],"NOT_IN_WB_API_CACHE")

    def test_linear_code_is_not_treated_as_9char_api_qr(self):
        response,status=self.diagnose(sticker="60227855344")
        self.assertEqual(status,200)
        self.assertEqual(response.json["reason"],"NOT_A_NINE_CHARACTER_WB_QR")

    def test_manager_permission_is_required(self):
        response,status=self.diagnose(authorized=False)
        self.assertEqual(status,403)

    def test_disabled_api_mode_does_not_fake_a_match(self):
        response,status=self.diagnose(allowed=False)
        self.assertEqual(status,503)


class ExplicitFbsSourceTests(unittest.TestCase):
    def test_v11_defaults_to_api_even_with_imported_pdf(self):
        import os
        import wms
        with patch.dict(os.environ,{"WB_FBS_V11_ENABLED":"1"},clear=False):
            with patch.dict(os.environ,{},clear=False):
                os.environ.pop("WB_FBS_SOURCE_MODE",None)
                with patch.object(wms,"pdf_picking_active",
                                  side_effect=AssertionError("Do not query PDF on API hot path")):
                    self.assertEqual(wms.fbs_selected_source("shift","badge"),"wb_api")

    def test_intentional_pdf_selection_with_v11_enabled(self):
        import os
        import wms
        with patch.dict(os.environ,{
            "WB_FBS_V11_ENABLED":"1","WB_FBS_SOURCE_MODE":"pdf"},clear=False):
            with patch.object(wms,"pdf_picking_active",return_value=True):
                self.assertEqual(wms.fbs_selected_source("shift","badge"),"pdf")

    def test_pdf_selection_without_imported_file_fails_closed(self):
        import os
        import wms
        with patch.dict(os.environ,{
            "WB_FBS_V11_ENABLED":"1","WB_FBS_SOURCE_MODE":"pdf"},clear=False):
            with patch.object(wms,"pdf_picking_active",return_value=False):
                self.assertEqual(wms.fbs_selected_source("shift","badge"),
                                 "unavailable")

    def test_invalid_source_configuration_fails_closed(self):
        import os
        import wms
        with patch.dict(os.environ,{
            "WB_FBS_V11_ENABLED":"1","WB_FBS_SOURCE_MODE":"anything"},clear=False):
            with patch.object(wms,"pdf_picking_active",
                              side_effect=AssertionError("Invalid mode cannot query data")):
                self.assertEqual(wms.fbs_selected_source("shift","badge"),
                                 "unavailable")

    def test_legacy_source_selection_remains_unchanged_when_v11_off(self):
        import os
        import wms
        with patch.dict(os.environ,{"WB_FBS_V11_ENABLED":"0"},clear=False):
            with patch.object(wms,"pdf_picking_active",return_value=True):
                self.assertEqual(wms.fbs_selected_source("shift","badge"),"pdf")
            with patch.object(wms,"pdf_picking_active",return_value=False):
                self.assertEqual(wms.fbs_selected_source("shift","badge"),"legacy")


if __name__=="__main__":
    unittest.main()
