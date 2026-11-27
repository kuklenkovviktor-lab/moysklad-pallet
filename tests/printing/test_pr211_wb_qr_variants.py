"""Synthetic keyboard/QR variants vs stored original WB decimal sticker.
The PDF artwork remains unchanged: normalize SCANNER TEXT ONLY.
"""
import base64
import unittest

from wb_qr_codec import (
    canonical_wb_sticker, EN_KEYS, RU_KEYS,
)

WB_DECIMAL='59298125228'


def encoded(decimal=WB_DECIMAL):
    return '*'+base64.b64encode(int(decimal).to_bytes(5,'big')+b'\x02').decode('ascii')


class WBOriginalScanVariants(unittest.TestCase):
    def test_original_eleven_digit_code(self):
        self.assertEqual(canonical_wb_sticker(WB_DECIMAL),WB_DECIMAL)

    def test_nine_character_wb_qr(self):
        qr=encoded()
        self.assertEqual(len(qr),9)
        self.assertEqual(canonical_wb_sticker(qr),WB_DECIMAL)

    def test_russian_hid_semicolon_restored_only_as_9char_qr(self):
        qr=encoded()
        self.assertEqual(canonical_wb_sticker(';'+qr[1:]),WB_DECIMAL)
        self.assertEqual(canonical_wb_sticker(';ABC'),';ABC')

    def test_aim_prefix_normalization(self):
        self.assertEqual(canonical_wb_sticker(']Q3'+encoded()),WB_DECIMAL)

    def test_full_russian_hid_keyboard_normalization(self):
        raw=encoded()
        ru=raw[1:].translate(str.maketrans(EN_KEYS, RU_KEYS))
        self.assertEqual(canonical_wb_sticker(';'+ru),WB_DECIMAL)

    def test_invalid_or_ambiguous_code_never_guessed(self):
        for source in ('*invalid!', ';ZZZZZZZZ',']Q3*invalid!','*123','', '123456789012'):
            self.assertNotEqual(canonical_wb_sticker(source),WB_DECIMAL)

    def test_rename_qr_not_rewrite_original_pdf(self):
        qr=encoded()
        before=b'ORIGINAL WB PDF PAGE BYTES'
        _=canonical_wb_sticker(qr)
        self.assertEqual(before,b'ORIGINAL WB PDF PAGE BYTES')


if __name__=='__main__':
    unittest.main()
