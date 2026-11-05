"""iOS Russian HID keyboard must not change identity or product codes."""
import ast
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1].joinpath("wms.py").read_text(encoding="utf-8")
MODULE = ast.parse(SOURCE)
FN = next(n for n in MODULE.body if isinstance(n, ast.FunctionDef) and n.name == "normalize_badge_input")
SCOPE = {}
exec(compile(ast.Module(body=[FN], type_ignores=[]), "badge_normalization", "exec"), SCOPE)
normalize = SCOPE["normalize_badge_input"]
RU = "йцукенгшщзхъфывапролджэячсмитьбю"
US = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
BADGE = "WMS-BADGE-a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4"


class BadgeKeyboardTests(unittest.TestCase):
    def test_cyrillic_hid_badge(self):
        typed = BADGE.lower().translate(str.maketrans(US, RU))
        self.assertTrue(typed.startswith("цьы-ифвпу-"))
        self.assertEqual(normalize(typed), BADGE)

    def test_normal_badge_unchanged(self):
        self.assertEqual(normalize(BADGE), BADGE)

    def test_numeric_scanner_badge_unchanged(self):
        code = "91012345678901234567"
        self.assertEqual(normalize(code), code)

    def test_unrelated_product_barcode_not_changed(self):
        self.assertEqual(normalize("2038719091772"), "2038719091772")

    def test_random_cyrillic_not_rewritten(self):
        self.assertEqual(normalize("ЩТШЧ-ЯУИКФ-КУФВН"), "ЩТШЧ-ЯУИКФ-КУФВН")

    def test_invalid_badge_not_rewritten(self):
        self.assertEqual(normalize("цьы-ифвпу-невалидный"), "цьы-ифвпу-невалидный")

    def test_prefix_only_is_not_valid_badge(self):
        self.assertEqual(normalize("цьы-ифвпу-"), "цьы-ифвпу-")


if __name__ == "__main__":
    unittest.main()
