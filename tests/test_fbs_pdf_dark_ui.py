"""Contrast regressions on the existing, read-only GRIT PDF intake UI."""
import unittest
from fbs_pdf_import_ui import FBS_PDF_PAGE

class PdfContrastTests(unittest.TestCase):
    def test_dark_details_and_previews(self):
        self.assertIn('id="grit-pdf-workspace"', FBS_PDF_PAGE)
        self.assertIn('#grit-pdf-workspace details>summary{', FBS_PDF_PAGE)
        self.assertIn('background:#20384f!important', FBS_PDF_PAGE)
        self.assertIn('#grit-pdf-workspace pre{', FBS_PDF_PAGE)
        self.assertIn('color:#f1f7ff!important', FBS_PDF_PAGE)

    def test_sticker_details_are_readable(self):
        self.assertIn('#grit-pdf-workspace #wbProducts td:last-child', FBS_PDF_PAGE)
        self.assertIn('#grit-pdf-workspace details .printcode{', FBS_PDF_PAGE)

    def test_upload_stays_read_only(self):
        self.assertIn("/api/manager/fbs-pdf/preview", FBS_PDF_PAGE)
        self.assertIn('Создание заданий отключено', FBS_PDF_PAGE)
        self.assertNotIn("/api/manager/fbs-pdf/physical-test-import", FBS_PDF_PAGE)

    def test_grit_brand(self):
        self.assertIn("GRIT WMS · Загрузка PDF FBS", FBS_PDF_PAGE)
        self.assertIn("Что GRIT прочитала", FBS_PDF_PAGE)

if __name__ == "__main__":
    unittest.main()
