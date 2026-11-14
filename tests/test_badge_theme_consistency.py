"""Static verification of GRIT badge-screen theme without any production data."""
import ast
from pathlib import Path
import unittest

SOURCE=Path(__file__).resolve().parents[1].joinpath("wms.py").read_text(encoding="utf-8")
TREE=ast.parse(SOURCE)
PAGE=next(node.value.value for node in TREE.body if isinstance(node,ast.Assign)
          and any(isinstance(t,ast.Name) and t.id=="BADGES_PAGE" for t in node.targets)
          and isinstance(node.value,ast.Constant) and isinstance(node.value.value,str))

class BadgeThemeTests(unittest.TestCase):
    def test_single_dark_page_background_and_panel_tokens(self):
        self.assertIn('content="#09121e"',PAGE)
        self.assertIn('--bg:#09121e',PAGE)
        self.assertIn('background:var(--bg)',PAGE)
        self.assertIn('--panel:#122438',PAGE)
        self.assertIn('--line:#314d64',PAGE)
        self.assertNotIn('background:#e9eef3',PAGE)
        self.assertNotIn('.create{background:#fff',PAGE)
        self.assertNotIn('.archived{background:#fff',PAGE)
        self.assertNotIn('.badge{background:#fff',PAGE)

    def test_qr_white_and_print_layout_is_preserved(self):
        self.assertIn('.qr{width:220px',PAGE)
        self.assertIn('background:#fff;padding:8px',PAGE)
        self.assertIn('@media print',PAGE)
        self.assertIn('background:#fff!important',PAGE)
        self.assertIn('body.print-single .badge.print-target',PAGE)
        self.assertIn('function printAllBadges()',PAGE)
        self.assertIn('function printBadge(id)',PAGE)

    def test_management_controls_still_present(self):
        for name in ('createEmployee','employeeName','archiveList',
                     'data-action="rename"','data-action="archive"','data-action="restore"'):
            self.assertIn(name,PAGE)

if __name__=="__main__":
    unittest.main()
