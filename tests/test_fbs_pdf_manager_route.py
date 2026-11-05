"""Manager-only access to PDF sandbox import; employee badge is not required."""
import unittest
from pathlib import Path
from unittest.mock import patch
from fbs_pdf_physical_test import physical_test_enabled

SOURCE=Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
PDF=Path(__file__).resolve().parents[1].joinpath("fbs_pdf_import.py").read_text(encoding="utf-8")

class PhysicalPdfManagerRoutingTests(unittest.TestCase):
    def test_import_in_manager_route_allowlist(self):
        section=SOURCE.split("manager_paths = {",1)[1].split("}",1)[0]
        self.assertIn('"/api/manager/fbs-pdf/preview"',section)
        self.assertIn('"/api/manager/fbs-pdf/physical-test-import"',section)

    def test_gateway_requires_manager_session(self):
        self.assertIn('if session.get("manager_web") is True:', SOURCE)
        self.assertIn('return jsonify(error="Требуется вход в ПК-панель"), 403', SOURCE)

    def test_endpoint_guards_both_role_and_isolation(self):
        segment=PDF.split('def fbs_pdf_physical_test_import():',1)[1]
        self.assertIn('if not manager_access():',segment)
        self.assertIn('if not physical_test_enabled():',segment)

if __name__=="__main__":
    unittest.main()
