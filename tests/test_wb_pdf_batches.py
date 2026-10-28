"""SQLite integration smoke tests for manager-only WB batch intake."""
import io
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from flask import Flask

TEMP = tempfile.TemporaryDirectory()
DB = os.path.join(TEMP.name, "test.db")

wms_stub = types.ModuleType("wms")
wms_stub.DATABASE_URL = ""
@contextmanager
def db_connect():
    conn = sqlite3.connect(DB)
    try:
        yield conn
    finally:
        conn.close()
wms_stub.db_connect = db_connect
wms_stub.manager_access = lambda: True
wms_stub.sql = lambda q: q
sys.modules["wms"] = wms_stub

labels_stub = types.ModuleType("fbs_pdf_labels")
labels_stub.parse_wb_labels = lambda doc: None
labels_stub.attach_active_locations = lambda report: report
sys.modules["fbs_pdf_labels"] = labels_stub
import wb_pdf_batches as batches


def make_report():
    return {
        "locations_available": True,
        "valid_structure": True,
        "unique_articles": 3,
        "unique_sticker_barcodes": 4,
        "products": [
            {"article":"1349_NONE","product_barcode":"2042741355108",
             "locations":["E-12"],"stickers":[{"barcode":"59393228243","page":2},{"barcode":"59393228244","page":3}]},
            {"article":"UNKNOWN","product_barcode":"1234567890123",
             "locations":[],"stickers":[{"barcode":"59393228245","page":5}]},
            {"article":"MULTI","product_barcode":"1234567890124",
             "locations":["A-01","B-01"],"stickers":[{"barcode":"59393228246","page":7}]},
        ],
    }


class BatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask(__name__)
        app.register_blueprint(batches.wb_batches_bp)
        cls.client = app.test_client()

    def upload(self, raw=b"%PDF-VALID", headers=None):
        return self.client.post("/api/manager/wb-batches/import",
            data={"file": (io.BytesIO(raw), "wb.pdf")},
            content_type="multipart/form-data",
            headers=headers or {"Origin":"https://onix.example"},
            base_url="https://onix.example")

    def test_import_and_idempotency(self):
        with patch.object(batches, "_parse", return_value=make_report()):
            first = self.upload()
            self.assertEqual(first.status_code, 201, first.get_json())
            data = first.get_json()
            self.assertEqual(data["sticker_count"], 4)
            self.assertEqual(data["print_jobs_created"], 0)
            self.assertEqual(data["routing"], {"located":2,"unassigned":1,"ambiguous":1})
            repeat = self.upload()
            self.assertEqual(repeat.status_code, 200, repeat.get_json())
            self.assertTrue(repeat.get_json()["duplicate"])
            self.assertEqual(repeat.get_json()["batch_id"], data["batch_id"])
        with db_connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_pdf_batches").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM wb_pdf_batch_stickers").fetchone()[0], 4)

    def test_invalid_origin_and_pdf(self):
        self.assertEqual(self.upload(headers={"Origin":"https://evil.example"}).status_code, 403)
        self.assertEqual(self.upload(raw=b"NOT A PDF").status_code, 400)

    def test_invalid_structure_rejected(self):
        with patch.object(batches, "_parse", side_effect=ValueError("invalid")):
            self.assertEqual(self.upload(raw=b"%PDF-OTHER").status_code, 422)


if __name__ == "__main__":
    unittest.main()
