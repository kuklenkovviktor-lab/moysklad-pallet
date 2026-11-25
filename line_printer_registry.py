"""Offline prototype: printer line registry and revocable 58x40 QR labels (#192).

No persistence, Flask routes, production printing or employee authentication.
The QR *identifies* a printer; it never grants employee access. Never log QR
payloads. Store only hashes; rotating a token invalidates older labels.
"""
import base64
from dataclasses import dataclass, replace
from hashlib import sha256
from hmac import compare_digest
from html import escape
from io import BytesIO
import re
import secrets

from line_printer_routing import line_of

_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,39}$")
_QR = re.compile(r"^GRIT-PRINTER:1:([a-zA-Z0-9_-]{1,40}):([a-zA-Z0-9_-]{40,80})$")


@dataclass(frozen=True)
class Printer:
    printer_id: str
    name: str
    lines: tuple[str, ...]
    token_digest: str
    enabled: bool = True


def normalize_lines(lines):
    if not isinstance(lines, (list, tuple, set, frozenset)) or not lines:
        raise ValueError("At least one line is required")
    result = set()
    for raw in lines:
        if not isinstance(raw, str):
            raise ValueError("Invalid line")
        line = raw.strip().upper()
        if line == "ПРОЧЕЕ" or not line or line_of(line + "-01") != line:
            raise ValueError("Invalid line")
        result.add(line)
    return tuple(sorted(result))


def create_printer(printer_id, name, lines):
    """Create an offline record and a one-time printable QR payload.

    Save record.token_digest, NOT the payload; label reprint requires rotation.
    """
    if not isinstance(printer_id, str) or not _ID.fullmatch(printer_id):
        raise ValueError("Invalid printer id")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Invalid printer name")
    normalized = normalize_lines(lines)
    token = secrets.token_urlsafe(32)
    payload = f"GRIT-PRINTER:1:{printer_id}:{token}"
    return Printer(printer_id, name.strip(), normalized,
                   sha256(payload.encode("utf-8")).hexdigest()), payload


def verify_printer_scan(payload, printer):
    """Verify only identity and activation, NOT employee permissions."""
    if not isinstance(payload, str) or len(payload) > 160:
        return False
    parsed = _QR.fullmatch(payload)
    if not parsed or not isinstance(printer, Printer) or not printer.enabled:
        return False
    if parsed.group(1) != printer.printer_id:
        return False
    digest = sha256(payload.encode("utf-8")).hexdigest()
    return compare_digest(digest, printer.token_digest)


def rotate_printer_token(printer):
    """Issue a new label; prior QR is immediately invalid against new record."""
    if not isinstance(printer, Printer):
        raise ValueError("Invalid printer")
    replacement, payload = create_printer(
        printer.printer_id, printer.name, printer.lines)
    return replace(replacement, enabled=printer.enabled), payload


def printable_58x40_html(printer, payload):
    """Render a standalone preview; no print() and no contact with hardware.

    Caller must guard access and ensure payload was generated in an approved
    manager-only workflow. Browser printing is explicit, never automatic.
    """
    if not verify_printer_scan(payload, printer):
        raise ValueError("QR does not belong to active printer")
    import qrcode
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M,
                       box_size=6, border=2)
    qr.add_data(payload)
    qr.make(fit=True)
    stream = BytesIO()
    qr.make_image(fill_color="black", back_color="white").save(stream, "PNG")
    qr64 = base64.b64encode(stream.getvalue()).decode("ascii")
    title = escape(printer.name)
    lines = escape(", ".join(printer.lines))
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>GRIT WMS · QR принтера 58×40</title>
<style>
@page{{size:58mm 40mm;margin:0}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:Arial,sans-serif}}
.label{{width:58mm;height:40mm;display:flex;align-items:center;
padding:1.5mm;gap:1mm;overflow:hidden;background:#fff;color:#000}}
.label img{{width:34mm;height:34mm;object-fit:contain;image-rendering:pixelated}}
.meta{{width:20mm;min-width:0;overflow-wrap:anywhere;font-size:8pt}}
.meta strong{{font-size:10pt}}
.meta small{{font-size:7pt;display:block;margin-top:2mm}}
@media print{{.label{{page-break-after:avoid}}}}
</style></head><body><article class="label"><img alt="QR принтера"
src="data:image/png;base64,{qr64}"><div class="meta"><strong>GRIT WMS</strong>
<p>{title}</p><small>Линии: {lines}</small></div></article></body></html>"""
