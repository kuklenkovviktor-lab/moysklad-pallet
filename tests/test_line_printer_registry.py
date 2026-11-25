"""Offline synthetic tests: printer registry, QR rotation and 58x40 label."""
import unittest

from line_printer_registry import (
    create_printer, normalize_lines, printable_58x40_html,
    rotate_printer_token, verify_printer_scan,
)


class PrinterRegistryTests(unittest.TestCase):
    def setUp(self):
        self.printer, self.payload = create_printer(
            "printer-01", "Принтер A-Б", ["Б", "А", "А"])

    def test_normalizes_and_deduplicates_lines(self):
        self.assertEqual(self.printer.lines, ("А", "Б"))
        self.assertEqual(normalize_lines(["a", "Б"]), ("A", "Б"))

    def test_qr_verifies_for_matching_active_printer(self):
        self.assertTrue(verify_printer_scan(self.payload, self.printer))
        self.assertNotIn(self.payload, repr(self.printer))
        self.assertNotIn(self.payload.split(":")[-1], repr(self.printer))

    def test_unrelated_printer_or_tampered_qr_rejected(self):
        another, other_qr = create_printer("printer-02", "Другой", ["А"])
        self.assertFalse(verify_printer_scan(self.payload, another))
        self.assertFalse(verify_printer_scan(other_qr, self.printer))
        self.assertFalse(verify_printer_scan(self.payload + "bad", self.printer))
        self.assertFalse(verify_printer_scan("GRIT-PRINTER:1:printer-01:bad", self.printer))

    def test_disabled_printer_rejected(self):
        from dataclasses import replace
        self.assertFalse(verify_printer_scan(
            self.payload, replace(self.printer, enabled=False)))

    def test_reissue_revokes_old_qr(self):
        new_printer, new_qr = rotate_printer_token(self.printer)
        self.assertFalse(verify_printer_scan(self.payload, new_printer))
        self.assertTrue(verify_printer_scan(new_qr, new_printer))
        self.assertEqual(new_printer.lines, self.printer.lines)

    def test_invalid_printers_and_lines_refused(self):
        for printer_id in ("", "../bad", "space name"):
            with self.subTest(printer_id=printer_id):
                with self.assertRaises(ValueError):
                    create_printer(printer_id, "Valid", ["А"])
        for lines in ([], ["ПРОЧЕЕ"], ["А-01"], ["123"], ["А", ""]):
            with self.subTest(lines=lines):
                with self.assertRaises(ValueError):
                    create_printer("printer-01", "Valid", lines)

    def test_label_dimensions_and_html_escaping(self):
        printer, qr = create_printer("print2", "<test & printer>", ["А", "Б"])
        html = printable_58x40_html(printer, qr)
        self.assertIn("size:58mm 40mm", html)
        self.assertIn("width:58mm;height:40mm", html)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("&lt;test &amp; printer&gt;", html)
        self.assertNotIn("<test & printer>", html)
        self.assertNotIn("window.print()", html)
        self.assertNotIn(qr, html)

    def test_old_qr_cannot_print_new_label(self):
        new_printer, _ = rotate_printer_token(self.printer)
        with self.assertRaises(ValueError):
            printable_58x40_html(new_printer, self.payload)


if __name__ == "__main__":
    unittest.main()
