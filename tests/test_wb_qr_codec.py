"""Offline regression using synthetic WB stickers, no production order IDs."""
import base64
import unittest
from wb_qr_codec import canonical_wb_sticker


def make_wb_qr(number, tail=0x36):
    return "*" + base64.b64encode(int(number).to_bytes(5, "big") + bytes([tail])).decode("ascii")


class WbQrCodecTests(unittest.TestCase):
    def test_qr_matches_printed_decimal(self):
        for numeric in (59000000001, 59123456789, 59500000011, 59999999999):
            with self.subTest(code=numeric):
                self.assertEqual(canonical_wb_sticker(make_wb_qr(numeric)), str(numeric))

    def test_last_byte_does_not_change_printed_order_id(self):
        for suffix in (0, 1, 36, 255):
            self.assertEqual(canonical_wb_sticker(make_wb_qr(59500000011,suffix)), "59500000011")

    def test_preserve_regular_printed_wb(self):
        self.assertEqual(canonical_wb_sticker("59500000011"), "59500000011")

    def test_hid_aim_prefix(self):
        self.assertEqual(canonical_wb_sticker("]Q3" + make_wb_qr(59500000011)), "59500000011")

    def test_ru_physical_keyboard(self):
        us="qwertyuiop[]asdfghjkl;'zxcvbnm,."
        ru="йцукенгшщзхъфывапролджэячсмитьбю"
        code=make_wb_qr(59500000011, 0x10)
        cyrillic=code.translate(str.maketrans(us+us.upper(),ru+ru.upper()))
        self.assertEqual(canonical_wb_sticker(cyrillic), "59500000011")

    def test_unknown_not_fabricated(self):
        for raw in ("", "not-wb", "*zz", "*notQRxx!", "2038719088932", "59112233445extra", "*%%%%%%%%"):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_wb_sticker(raw), raw)

    def test_numeric_out_of_range_rejected(self):
        q="*" + base64.b64encode((123).to_bytes(5, "big")+b"\x99").decode("ascii")
        self.assertEqual(canonical_wb_sticker(q),q)

    def test_real_scan_branch_is_pdf_scoped(self):
        from pathlib import Path
        s=Path(__file__).resolve().parents[1].joinpath("wms.py").read_text(encoding="utf-8")
        before=s.split('def fbs_scan():',1)[1].split('duplicate = query_one(',1)[0]
        self.assertIn('if pdf_control:',before)
        self.assertIn('barcode = canonical_wb_sticker(barcode)',before)
        self.assertIn('SELECT article, product_barcode FROM pdf_fbs_orders WHERE wb_barcode=?',before)


if __name__=="__main__":
    unittest.main()
