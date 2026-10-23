"""Regressions for compact 58 x 40 mm QR-only thermal cell labels."""
import base64
import io
import os
import re
import tempfile
import unittest
import uuid

os.environ.setdefault("WMS_BADGE_SECRET", "test-badge")
os.environ.setdefault("WMS_TERMINAL_SECRET", "test-terminal")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("MOYSKLAD_TOKEN", "test-only")
os.environ["WMS_REQUIRE_POSTGRES"] = "0"
os.environ.pop("DATABASE_URL", None)
_TEMP = tempfile.TemporaryDirectory()
os.environ["WMS_DB_PATH"] = os.path.join(_TEMP.name, "qr-cell-test.sqlite")

import app
import qrcode
from wms import execute, init_wms_db, query_one


class QrCellLabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_wms_db()
        app._WMS_READY.set()

    def setUp(self):
        self.client = app.app.test_client()
        with self.client.session_transaction() as session:
            session.clear()
            session["manager_web"] = True
        self.id = str(uuid.uuid4())
        self.label = "А-01"
        self.barcode = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
        execute("""
            INSERT INTO replenishment_locations
                (id, label, barcode, created_at, active)
            VALUES (?, ?, ?, ?, 1)
        """, (self.id, self.label, self.barcode, "2026-10-23T00:00:00Z"))

    def get_label(self):
        return self.client.get("/manager/replenishment/label?id=" + self.id)

    @staticmethod
    def expected_png(payload):
        qr = qrcode.QRCode(
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=16, border=4,
        )
        qr.add_data(payload)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        b = io.BytesIO()
        img.save(b, format="PNG")
        return b.getvalue()

    def test_prints_qr_payload_unchanged_for_existing_location(self):
        r = self.get_label()
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn('QR ячейки', text)
        match = re.search(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"', text)
        self.assertIsNotNone(match, text[:500])
        png = base64.b64decode(match.group(1))
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(png, self.expected_png(self.barcode))
        self.assertEqual(query_one(
            "SELECT barcode FROM replenishment_locations WHERE id=?",
            (self.id,))[0], self.barcode)

    def test_print_css_and_label_text(self):
        r = self.get_label()
        html = r.get_data(as_text=True)
        self.assertIn("@page {size:58mm 40mm; margin:0}", html)
        self.assertIn("width:58mm;height:40mm", html)
        self.assertIn("width:30mm;height:30mm", html)
        self.assertIn('class="address">А-01</div>', html)
        self.assertNotIn('JsBarcode', html)
        self.assertNotIn("ONIX WMS · ЯЧЕЙКА", html)
        self.assertNotIn(self.barcode, html)
        self.assertIn('window.print()', html)

    def test_nonmanager_cannot_print_registered_location(self):
        with self.client.session_transaction() as session:
            session.clear()
            session["employee_id"] = "anton"
        r = self.get_label()
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/manager-login"))

    def test_missing_location_does_not_make_qr(self):
        missing = self.client.get("/manager/replenishment/label?id=not-a-real-cell")
        self.assertEqual(missing.status_code, 404)

    def test_address_text_is_html_escaped(self):
        location = str(uuid.uuid4())
        bad = "<script>alert(1)</script>"
        code = "ONIX-LOC-" + uuid.uuid4().hex[:16].upper()
        execute("""
            INSERT INTO replenishment_locations
                (id, label, barcode, created_at, active)
            VALUES (?, ?, ?, ?, 1)
        """, (location, bad, code, "2026-10-23T00:00:00Z"))
        result = self.client.get("/manager/replenishment/label?id=" + location)
        self.assertEqual(result.status_code, 200)
        self.assertIn("&lt;script&gt;", result.get_data(as_text=True))
        self.assertNotIn("<script>alert(1)</script>", result.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
