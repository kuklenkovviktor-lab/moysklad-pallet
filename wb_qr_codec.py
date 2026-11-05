"""Canonicalize Wildberries FBS sticker QR to its printed 11-digit barcode.

Physical WB stickers have different symbologies: the Code128 barcode contains
the printed decimal order ID, whereas QR stores '*' + Base64(6 bytes), where
the first five bytes are the same 40-bit decimal ID and the last byte is an
additional WB field. Conversion is used ONLY at the WB sticker scan stage,
never at the item-barcode or employee-badge stage.
"""
import base64
import binascii
import re

RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбю"
EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
RU_HID_TO_EN = str.maketrans(
    RU_KEYS + RU_KEYS.upper(), EN_KEYS + EN_KEYS.upper()
)
QR_DATA_RE = re.compile(r"\*[A-Za-z0-9+/]{8}\Z")


def canonical_wb_sticker(value):
    code = str(value or "").strip()
    if re.fullmatch(r"\d{11}", code):
        return code
    # Scanner AIM symbology ID, if configured to send it.
    if len(code) > 3 and re.fullmatch(r"\]Q\d", code[:3]):
        code = code[3:]
    if code.startswith("*") and len(code) == 9:
        candidate = code.translate(RU_HID_TO_EN)
        if QR_DATA_RE.fullmatch(candidate):
            try:
                payload = base64.b64decode(candidate[1:], validate=True)
            except (binascii.Error, ValueError):
                return code
            if len(payload) == 6:
                numeric = int.from_bytes(payload[:5], "big")
                if 10**10 <= numeric < 10**11:
                    return str(numeric)
    return code
