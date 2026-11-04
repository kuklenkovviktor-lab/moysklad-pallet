"""Regression checks: worker UI polish must not change GRIT warehouse operation flows."""
from pathlib import Path
import re
import unittest


class GritWorkerPolishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.wms = Path("wms.py").read_text(encoding="utf-8")
        cls.rep = Path("replenishment.py").read_text(encoding="utf-8")

    def test_fbs_scan_flow_intact(self):
        page = self.wms.split('FBS_PAGE = """', 1)[1].split('"""', 1)[0]
        for token in (
            'id="scanStageBox"', 'id="barcode"', 'id="scanButton"',
            'id="resetProduct"', 'id="tsdError"', 'function handleScan(code)',
            'function acceptProduct(code)', 'function acceptWb(code)',
            "if(pendingProduct)await acceptWb(code);",
            "'/api/fbs/product-info'", "'/api/fbs/scan'",
            "'/api/fbs/stats?shift_id='",
        ):
            self.assertIn(token, page)
        self.assertIn('#scanCard:has(#resetProduct:not(.hidden)) .scan-stage', page)
        self.assertIn('button:focus-visible', page)
        self.assertIn('@media (orientation:landscape) and (max-height:600px)', page)
        self.assertEqual(page.count("<style>"), 1)
        self.assertEqual(page.count("</style>"), 1)

    def test_replenishment_worker_only_is_dark(self):
        page = self.rep.split('WORKER_PAGE = """', 1)[1].split('"""', 1)[0]
        manager_page = self.rep.split('MANAGER_PAGE = """', 1)[1].split('"""', 1)[0]
        self.assertIn(':root{color-scheme:dark;', page)
        self.assertIn('body{background:var(--g-bg);', page)
        self.assertNotIn('GRIT warehouse-worker skin', manager_page)
        self.assertIn("function focusTaskInput(", page)
        self.assertIn("function verifyScan(", page)
        self.assertIn("function createSelfTask(", page)
        self.assertIn('id="cameraOverlay"', page)
        self.assertIn('id="repReader"', page)
        self.assertIn('id="selfLocation"', page)
        self.assertIn('id="selfBarcode"', page)
        self.assertIn('#msg:not(:empty)', page)
        self.assertIn('button:focus-visible', page)
        self.assertEqual(page.count("<style>"), 1)
        self.assertEqual(page.count("</style>"), 1)

    def test_no_external_assets_required_for_new_styles(self):
        for page in (self.wms.split('FBS_PAGE = """', 1)[1].split('"""', 1)[0],
                     self.rep.split('WORKER_PAGE = """', 1)[1].split('"""', 1)[0]):
            self.assertNotIn('url(http', page.lower())
            self.assertNotIn('<iframe', page.lower())


if __name__ == "__main__":
    unittest.main()
