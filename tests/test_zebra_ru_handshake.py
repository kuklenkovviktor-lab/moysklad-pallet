"""Regression: Russian iPhone HID input recognizes only the Zebra readiness barcode."""
import re
import unittest
from pathlib import Path

WMS_SOURCE = Path(__file__).resolve().parents[1].joinpath("wms.py").read_text(encoding="utf-8")


class ZebraHandshakeLayoutTest(unittest.TestCase):
    def test_handshake_supports_exact_russian_keyboard_payload(self):
        self.assertIn("if(code==='ЩТШЧ-ЯУИКФ-КУФВН')return 'ONIX-ZEBRA-READY';", WMS_SOURCE)

    def test_normalizer_applies_to_both_keyboard_capture_paths(self):
        self.assertIn("const code=normalizeHidCode(hidBadge.value||hidBuffer);", WMS_SOURCE)
        self.assertIn("const code=normalizeHidCode(hidBuffer);", WMS_SOURCE)
        self.assertIn("if(code===ZEBRA_SERVICE_CODE)", WMS_SOURCE)

    def test_existing_login_stays_after_ready_gate(self):
        self.assertIn("if(!zebraReady)", WMS_SOURCE)
        self.assertIn("login(code);", WMS_SOURCE)


if __name__ == "__main__":
    unittest.main()
