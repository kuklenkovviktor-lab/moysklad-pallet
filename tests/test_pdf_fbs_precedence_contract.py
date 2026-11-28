"""Regression guard for PDF/WT API scanner mode precedence.

These assertions protect the two-layer contract in the HTTP scan handler and
the transactional commit. Full integration tests still require isolated WMS DB.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PdfFbsPrecedenceContractTests(unittest.TestCase):
    @staticmethod
    def source_function(file, function):
        tree = ast.parse((ROOT / file).read_text(encoding="utf-8"))
        node = next(
            n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and n.name == function
        )
        return ast.get_source_segment((ROOT / file).read_text(encoding="utf-8"), node)

    def test_http_scan_pdf_overrides_api_mode(self):
        source = self.source_function("wms.py", "fbs_scan")
        self.assertIn('pdf_control = pdf_picking_active(shift_id, employee["id"])', source)
        self.assertIn('if os.getenv("WB_FBS_V11_ENABLED", "0") == "1" and not pdf_control:', source)
        self.assertIn("if request_id and not pdf_control:", source)
        self.assertLess(source.index("if request_id and not pdf_control:"),
                        source.index("if pdf_control:\n        pdf_expected = query_one("))
        self.assertIn('error="❌ Стикер WB отсутствует в загруженном PDF.', source)
        self.assertIn('reason="pdf_product_mismatch"', source)

    def test_transaction_revalidates_pdf_even_with_api_mode_enabled(self):
        source = self.source_function("fbs_transactions.py", "commit_scan")
        self.assertIn("from wms import pdf_picking_active", source)
        self.assertIn('v11 = os.getenv("WB_FBS_V11_ENABLED", "0") == "1" and not pdf_picking_active(shift_id, employee_id)', source)
        self.assertIn("if not v11 and cur.fetchone():", source)
        self.assertIn("expected[1] != product_barcode", source)
        self.assertIn("wb_order_id = 'pdf:' + barcode", source)


if __name__ == "__main__":
    unittest.main()
