"""Regression tests for safe, diagnostic-only PDF uploads in ONIX."""
import io
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("SECRET_KEY", "pdf-test-flask-secret")
os.environ.setdefault("WMS_BADGE_SECRET", "pdf-test-badge-secret")
os.environ.setdefault("WMS_TERMINAL_SECRET", "pdf-test-terminal-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "pdf-upload-tests.db")

import fitz
import app
from fbs_pdf_import import (
    MAX_BYTES, MAX_PAGES, _sample_indices, inspect_pdf,
)
from wms import query_one, ensure_db


def build_pdf(texts):
    doc = fitz.open()
    for value in texts:
        page = doc.new_page(width=210, height=145)
        if value:
            page.insert_text((14, 35), value, fontsize=10)
    out = doc.tobytes()
    doc.close()
    return out


class PdfUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ensure_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session.clear()
            session["manager_web"] = True

    def upload(self, content, filename="orders.pdf"):
        return self.client.post(
            "/api/manager/fbs-pdf/preview",
            data={"file": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    def test_manager_can_upload_and_preview_but_never_create_orders(self):
        sample=build_pdf(["seller sku: SKU_1349  barcode: 1234567890123", "Товар: Кружка, 2 шт."])
        before = query_one("SELECT COUNT(*) FROM wb_orders")[0]
        response=self.upload(sample, "orders.pdf")
        self.assertEqual(response.status_code, 200, response.json)
        data=response.json
        self.assertEqual(data["filename"], "orders.pdf")
        self.assertEqual(data["pages"], 2)
        self.assertEqual(data["sampled_pages"], 2)
        self.assertEqual(data["text_pages_in_sample"], 2)
        self.assertEqual(data["mode"], "diagnostic_only")
        self.assertEqual(data["orders_imported"], 0)
        self.assertEqual(data["tasks_created"], 0)
        self.assertFalse(data["stock_changed"])
        self.assertFalse(data["file_stored"])
        self.assertEqual(data["page_samples"][0]["number"], 1)
        self.assertIn("SKU_1349", data["page_samples"][0]["excerpt"])
        self.assertIn("SKU_1349", data["article_hints"])
        self.assertEqual(query_one("SELECT COUNT(*) FROM wb_orders")[0], before)

    def test_empty_text_page_is_reported_as_unreadable_not_as_zero_orders(self):
        response=self.upload(build_pdf([""]))
        self.assertEqual(response.status_code,200,response.json)
        data=response.json
        self.assertEqual(data["text_pages_in_sample"],0)
        self.assertEqual(data["page_samples"][0]["lines"],[])
        self.assertTrue(any("нет извлекаемого текста" in x for x in data["warnings"]))
        self.assertEqual(data["orders_imported"],0)

    def test_invalid_extension_and_fake_pdf_are_rejected(self):
        fake=self.upload(b"not a pdf")
        self.assertEqual(fake.status_code,400)
        self.assertIn("не PDF", fake.json["error"])
        wrong=self.upload(build_pdf(["OK"]), filename="orders.txt")
        self.assertEqual(wrong.status_code,400)

    def test_document_with_password_is_rejected(self):
        doc=fitz.open()
        doc.new_page().insert_text((20,30),"Secret", fontsize=11)
        payload=doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="1234",
        )
        doc.close()
        response=self.upload(payload)
        self.assertEqual(response.status_code,400,response.json)
        self.assertIn("паролем",response.json["error"])

    def test_no_manager_session_cannot_view_or_upload(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["employee_id"] = "anton"
        view=self.client.get("/manager/fbs-pdf")
        self.assertEqual(view.status_code,302)
        self.assertIn("/manager-login",view.location)
        upload=self.upload(build_pdf(["test"]))
        self.assertEqual(upload.status_code,403)

    def test_file_size_is_bounded_without_parsing(self):
        oversized = b"%PDF-" + b" " * MAX_BYTES
        with self.assertRaisesRegex(ValueError, "слишком большой"):
            inspect_pdf(oversized)
        self.assertGreater(MAX_PAGES,4000)

    def test_sampling_retains_start_middle_end_and_is_bounded(self):
        sample=_sample_indices(5000)
        self.assertLessEqual(len(sample),30)
        self.assertIn(0,sample)
        self.assertIn(2500,sample)
        self.assertIn(4999,sample)
        self.assertEqual(_sample_indices(2),[0,1])

    def test_manager_import_pdf_then_employee_scans_strictly(self):
        """End-to-end HTTP test: manager PDF -> automatic scan enforcement."""
        sticker_a="59800011001"
        sticker_b="59800011002"
        barcode_a="2038719091772"
        barcode_b="2038719091773"
        expected={"valid_structure":True,"unique_sticker_barcodes":2,
                  "products":[
                      {"article":"TEST_PDF_A","wb_article":"123456789",
                       "product_barcode":barcode_a,"stickers":[{"barcode":sticker_a}]},
                      {"article":"TEST_PDF_B","wb_article":"123456790",
                       "product_barcode":barcode_b,"stickers":[{"barcode":sticker_b}]},
                  ]}
        valid={"wb_report":expected,"sha256":"a"*64}
        from wms import query_one
        before_wb=query_one("SELECT COUNT(*) FROM wb_orders")[0]
        with patch("fbs_pdf_import.inspect_pdf",return_value=valid):
            for expected_new in (2,0):
                response=self.client.post(
                    "/api/manager/fbs-pdf/import",
                    data={"file":(io.BytesIO(b"%PDF-1.4"),"test.pdf")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json["orders_imported"],expected_new)
                self.assertFalse(response.json["stock_changed"])
        self.assertEqual(query_one("SELECT COUNT(*) FROM wb_orders")[0],before_wb)
        self.assertEqual(query_one(
            "SELECT product_barcode FROM pdf_fbs_orders WHERE wb_barcode=?",
            (sticker_a,))[0],barcode_a)

        worker=app.app.test_client()
        with worker.session_transaction() as session:
            session.clear()
            session["employee_id"]="ilya"
        shift_response=worker.post("/api/fbs/start",json={})
        self.assertEqual(shift_response.status_code,200,shift_response.json)
        shift_id=shift_response.json["shift_id"]
        # Employee interface has no PDF toggle or mode API.
        self.assertNotIn("pdfModeButton", worker.get("/fbs").get_data(as_text=True))
        self.assertNotIn("togglePdfMode", worker.get("/fbs").get_data(as_text=True))
        denied_switch=worker.post("/api/fbs/pdf-mode",json={"shift_id":shift_id,"enabled":False})
        self.assertEqual(denied_switch.status_code,404)
        from wms import pdf_picking_active, execute
        self.assertTrue(pdf_picking_active(shift_id,"ilya"))
        # A leftover OFF setting from earlier releases cannot bypass protection.
        execute(
            "INSERT INTO pdf_fbs_shift_modes (shift_id, enabled, updated_at) "
            "VALUES (?, 0, ?) ON CONFLICT(shift_id) DO UPDATE SET enabled=0",
            (shift_id,"2026-11-05T00:00:00+00:00"),
        )
        self.assertTrue(pdf_picking_active(shift_id,"ilya"))
        with patch("app.find_fbs_product_by_barcode",side_effect=AssertionError("must not call MoySklad")):
            a=worker.post("/api/fbs/product-info",json={"barcode":barcode_a,"shift_id":shift_id})
        self.assertEqual(a.status_code,200,a.json)
        self.assertEqual(a.json["article"],"TEST_PDF_A")
        args={"shift_id":shift_id,"product_barcode":barcode_b,
              "barcode":sticker_a,"article":"TEST_PDF_B","product_name":"Test"}
        wrong=worker.post("/api/fbs/scan",json=args)
        self.assertEqual(wrong.status_code,409,wrong.json)
        self.assertEqual(wrong.json["reason"],"pdf_product_mismatch")
        missing=worker.post("/api/fbs/scan",json={**args,"barcode":"59800011999"})
        self.assertEqual(missing.status_code,409,missing.json)
        self.assertEqual(missing.json["reason"],"pdf_order_missing")
        good=worker.post("/api/fbs/scan",json={
            **args,"barcode":sticker_a,"product_barcode":barcode_a,"article":"TEST_PDF_A"
        })
        self.assertEqual(good.status_code,200,good.json)
        duplicate=worker.post("/api/fbs/scan",json={
            **args,"barcode":sticker_a,"product_barcode":barcode_a,"article":"TEST_PDF_A"
        })
        self.assertEqual(duplicate.status_code,409,duplicate.json)
        self.assertEqual(query_one("SELECT COUNT(*) FROM scans WHERE barcode=?",(sticker_a,))[0],1)

    def test_pdf_import_requires_manager_and_rejects_conflicting_mapping(self):
        from pdf_fbs_registry import prepare_pdf_orders
        good={"valid_structure":True,"unique_sticker_barcodes":1,
              "products":[{"article":"TEST_1","wb_article":"123456789",
                           "product_barcode":"2038719091761",
                           "stickers":[{"barcode":"59800011888"}]}]}
        self.assertEqual(len(prepare_pdf_orders(good)),1)
        with self.client.session_transaction() as s:
            s.clear()
        denied=self.client.post("/api/manager/fbs-pdf/import",
            data={"file":(io.BytesIO(b"dummy"),"label.pdf")},
            content_type="multipart/form-data")
        self.assertEqual(denied.status_code,403)
        with self.client.session_transaction() as s:
            s["manager_web"]=True
        with patch("fbs_pdf_import.inspect_pdf",return_value={
            "wb_report":good,"sha256":"b"*64
        }):
            first=self.client.post("/api/manager/fbs-pdf/import",
                data={"file":(io.BytesIO(b"dummy"),"label.pdf")},
                content_type="multipart/form-data")
        self.assertEqual(first.status_code,200,first.json)
        conflict={"valid_structure":True,"unique_sticker_barcodes":1,
              "products":[{"article":"TEST_2","wb_article":"123456789",
                           "product_barcode":"2038719091788",
                           "stickers":[{"barcode":"59800011888"}]}]}
        with patch("fbs_pdf_import.inspect_pdf",return_value={
            "wb_report":conflict,"sha256":"c"*64
        }):
            rejected=self.client.post("/api/manager/fbs-pdf/import",
                data={"file":(io.BytesIO(b"dummy"),"label.pdf")},
                content_type="multipart/form-data")
        self.assertEqual(rejected.status_code,422,rejected.json)
        self.assertEqual(query_one(
            "SELECT article FROM pdf_fbs_orders WHERE wb_barcode=?",
            ("59800011888",))[0],"TEST_1")

    def test_manager_screen_explains_no_write_semantics(self):
        response=self.client.get("/manager/fbs-pdf")
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('id="pdf"',html)
        self.assertIn('/api/manager/fbs-pdf/preview',html)
        self.assertIn("Никаких заданий",html)


if __name__=="__main__":
    unittest.main()
