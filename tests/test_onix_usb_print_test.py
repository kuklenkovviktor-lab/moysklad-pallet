"""Hardware-free safety tests for isolated XP-460B Windows pilot."""
import contextlib
import io
import unittest

import onix_usb_print_test as pilot


class FakeSpooler:
    PRINTER_ENUM_LOCAL = 2
    PRINTER_ENUM_CONNECTIONS = 4

    def __init__(self, available=None, default="FBS-XP460B", bytes_written=None):
        self.available = available or ["FBS-XP460B", pilot.TEST_PRINTER_NAME]
        self.default = default
        self.events = []
        self.bytes_written = bytes_written

    def EnumPrinters(self, flags, server, level):
        return [{"pPrinterName": name} for name in self.available]

    def GetDefaultPrinter(self):
        return self.default

    def OpenPrinter(self, name):
        self.events.append(("open", name))
        return "handle"

    def StartDocPrinter(self, handle, level, details):
        self.events.append(("start_doc", details))
        return 1

    def StartPagePrinter(self, handle):
        self.events.append(("start_page", handle))

    def WritePrinter(self, handle, data):
        self.events.append(("write", data))
        return len(data) if self.bytes_written is None else self.bytes_written

    def EndPagePrinter(self, handle):
        self.events.append(("end_page", handle))

    def EndDocPrinter(self, handle):
        self.events.append(("end_doc", handle))

    def ClosePrinter(self, handle):
        self.events.append(("close", handle))


class PrinterPilotTests(unittest.TestCase):
    def test_dry_run_never_prints(self):
        fake = FakeSpooler()
        with contextlib.redirect_stdout(io.StringIO()):
            result = pilot.main(["--printer", pilot.TEST_PRINTER_NAME], spooler=fake)
        self.assertEqual(result, 0)
        self.assertEqual(fake.events, [])

    def test_prints_exactly_one_inert_label_on_explicit_target(self):
        fake = FakeSpooler()
        with contextlib.redirect_stdout(io.StringIO()):
            result = pilot.main(
                ["--printer", pilot.TEST_PRINTER_NAME, "--send-one-test"], spooler=fake
            )
        self.assertEqual(result, 0)
        self.assertEqual([e[0] for e in fake.events].count("write"), 1)
        self.assertEqual(fake.events[0], ("open", pilot.TEST_PRINTER_NAME))
        self.assertEqual(fake.events[-1], ("close", "handle"))
        data = next(e[1] for e in fake.events if e[0] == "write")
        self.assertIn(b"SIZE 40 mm,30 mm", data)
        self.assertIn(b"ONIX TEST", data)
        self.assertIn(b"NOT WB", data)
        self.assertIn(b"ONIX-TEST-ONLY", data)
        self.assertEqual(data.count(b"PRINT 1,1"), 1)

    def test_refuses_working_printer_even_when_explicitly_requested(self):
        fake = FakeSpooler()
        with contextlib.redirect_stderr(io.StringIO()):
            result = pilot.main(
                ["--printer", "FBS-XP460B", "--send-one-test"], spooler=fake
            )
        self.assertEqual(result, 1)
        self.assertFalse(fake.events)

    def test_refuses_default_printer(self):
        fake = FakeSpooler(default=pilot.TEST_PRINTER_NAME)
        with contextlib.redirect_stderr(io.StringIO()):
            result = pilot.main(
                ["--printer", pilot.TEST_PRINTER_NAME, "--send-one-test"], spooler=fake
            )
        self.assertEqual(result, 1)
        self.assertEqual(fake.events, [])

    def test_refuses_missing_printer(self):
        fake = FakeSpooler(available=["FBS-XP460B"])
        with contextlib.redirect_stderr(io.StringIO()):
            result = pilot.main(
                ["--printer", pilot.TEST_PRINTER_NAME, "--send-one-test"], spooler=fake
            )
        self.assertEqual(result, 1)
        self.assertEqual(fake.events, [])

    def test_partial_write_errors_with_no_retries(self):
        fake = FakeSpooler(bytes_written=2)
        with contextlib.redirect_stderr(io.StringIO()):
            result = pilot.main(
                ["--printer", pilot.TEST_PRINTER_NAME, "--send-one-test"], spooler=fake
            )
        self.assertEqual(result, 1)
        self.assertEqual([e[0] for e in fake.events].count("write"), 1)
        self.assertEqual(fake.events[-1], ("close", "handle"))

    def test_listing_only_does_not_touch_spooler_queue(self):
        fake = FakeSpooler()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = pilot.main(["--list"], spooler=fake)
        self.assertEqual(result, 0)
        self.assertIn("FBS-XP460B (по умолчанию)", output.getvalue())
        self.assertIn(pilot.TEST_PRINTER_NAME, output.getvalue())
        self.assertEqual(fake.events, [])


if __name__ == "__main__":
    unittest.main()
