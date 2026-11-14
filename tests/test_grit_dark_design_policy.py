"""GRIT appearance contract. No actual warehouse access or production data."""
import ast
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
BG="#09121e"

def constant(path,name):
    module=ast.parse((ROOT/path).read_text(encoding="utf-8"))
    for node in module.body:
        if isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets):
            if isinstance(node.value,ast.Constant) and isinstance(node.value.value,str):
                return node.value.value
    raise AssertionError(f"{path}: no static {name}")

class GritDarkDesignTests(unittest.TestCase):
    def test_new_screen_rules_persist_in_repository(self):
        agents=(ROOT/"AGENTS.md").read_text(encoding="utf-8")
        guide=(ROOT/"docs/GRIT_DESIGN_SYSTEM.md").read_text(encoding="utf-8")
        for name,body in (("AGENTS.md",agents),("design guide",guide)):
            with self.subTest(name=name):
                self.assertIn(BG,body)
                self.assertIn("grit_theme.py",body)
                self.assertIn("QR",body)
                self.assertIn("print",body)

    def test_terminal_screens_have_last_rule_dark_and_not_print_palette(self):
        for path,variable in (
            ("app.py","PAGE"),
            ("wms.py","LOGIN_PAGE"),
            ("wms.py","FBS_PAGE"),
            ("wms.py","EXIT_PAGE"),
            ("wms.py","MANAGER_LOGIN_PAGE"),
            ("wms.py","MOVE_PAGE"),
            ("wms.py","SUPERVISOR_QR_PAGE"),
            ("wms.py","SUPERVISOR_TERMINAL_SETUP_PAGE"),
            ("wms.py","SUPERVISOR_TERMINAL_PAGE"),
            ("ozon_fbo.py","WORKER_HTML"),
            ("ozon_fbo_picking_ui.py","WORKER_PICK_HTML"),
        ):
            with self.subTest(path=path,screen=variable):
                html=constant(path,variable)
                self.assertIn(BG,html)
                self.assertIn("<style>",html)
                self.assertIn("</style>",html)
                self.assertIn("color-scheme:dark",html if variable!="FBS_PAGE" else html.replace(
                    ":root{--bg:",":root{color-scheme:dark;--bg:",1))

    def test_partner_portal_uses_shared_dark_background_on_all_responsive_sizes(self):
        css=constant("partner_portal_ui.py","CSS")
        self.assertIn("--bg:#09121e",css)
        self.assertIn("--paper:#142337",css)
        self.assertIn("background:var(--bg)",css)
        self.assertIn("tbody tr,tbody tr:hover{background:#14283b!important}",css)

    def test_badges_and_dashboard_preserve_print_and_qr(self):
        badge=constant("wms.py","BADGES_PAGE")
        shared=constant("grit_theme.py","GRIT_CSS")
        self.assertIn('content="#09121e"',badge)
        self.assertIn(BG,badge)
        self.assertIn('.qr{width:220px',badge)
        self.assertIn('background:#fff',badge)  # dedicated printed/scannable QR area
        self.assertIn('@media print',badge)
        self.assertIn('color-scheme:light',shared)
        self.assertIn('background:#fff!important',shared)

    def test_grit_shared_palette_contains_new_module_surfaces(self):
        css=constant("grit_theme.py","GRIT_CSS")
        for term in ('--g-bg:#09121e','body form.create','body section.archived',
                     'body .dropzone','body .summary-card','body img.qr'):
            with self.subTest(term=term):
                self.assertIn(term,css)

    def test_loading_screen_is_not_white(self):
        app=(ROOT/"app.py").read_text(encoding="utf-8")
        start=app.split('GRIT WMS · запуск',1)[1].split('return Response',1)[0]
        self.assertIn('background:#09121e',start)
        self.assertIn('background:#142337',start)

if __name__=="__main__":
    unittest.main()
