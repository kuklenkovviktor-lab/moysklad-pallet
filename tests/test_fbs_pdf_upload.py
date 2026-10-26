"""Regression tests for safe, diagnostic-only PDF uploads in ONIX."""
import io
import os
import tempfile
import unittest

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

    def test_manager_screen_explains_no_write_semantics(self):
        response=self.client.get("/manager/fbs-pdf")
        self.assertEqual(response.status_code,200)
        html=response.get_data(as_text=True)
        self.assertIn('id="pdf"',html)
        self.assertIn('/api/manager/fbs-pdf/preview',html)
        self.assertIn("Никаких заданий",html)


if __name__=="__main__":
    unittest.main()
