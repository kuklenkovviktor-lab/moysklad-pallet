"""Manual, local proof print of ONE original WB sticker page from a PDF.

This tool never uses cloud print jobs, creates WMS orders, or changes stock.
Preview is compulsory. A persistent local lock prevents retrying uncertain jobs.
"""
import argparse
import hashlib
import json
import os
import re
import struct
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

from fbs_pdf_labels import parse_wb_labels
from onix_usb_print_test import (
    TEST_PRINTER_NAME, PrintPilotError, _win32print,
    verify_safe_test_destination,
)

WIDTH_DOTS = 320
HEIGHT_DOTS = 240
BYTES_PER_ROW = WIDTH_DOTS // 8
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 10000
WB_PATTERN = re.compile(r"\d{11}\Z")


class PilotError(ValueError):
    pass


def read_and_select(pdf_path, wb_code):
    if not WB_PATTERN.fullmatch(wb_code):
        raise PilotError("Нужен точный 11-значный ШК стикера Wildberries.")
    path = Path(pdf_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise PilotError("Укажи исходный PDF-файл WB.")
    if path.stat().st_size > MAX_PDF_BYTES:
        raise PilotError("Файл больше 25 МБ.")
    raw = path.read_bytes()
    if not raw.startswith(b"%PDF-"):
        raise PilotError("Неверный формат PDF.")
    digest = hashlib.sha256(raw).hexdigest()

    import pymupdf as fitz
    with fitz.open(stream=raw, filetype="pdf") as doc:
        if doc.needs_pass or not 0 < doc.page_count <= MAX_PAGES:
            raise PilotError("PDF защищён паролем, пуст или слишком длинный.")
        report = parse_wb_labels(doc)
        if not report or not report["valid_structure"]:
            raise PilotError(
                "PDF не прошёл полную проверку ONIX. Печатать стикеры нельзя."
            )
        found = [
            (item, sticker)
            for item in report["products"]
            for sticker in item["stickers"]
            if sticker["barcode"] == wb_code
        ]
        if len(found) != 1:
            raise PilotError("ШК отсутствует в PDF или встречается неоднозначно.")
        product, sticker = found[0]
        page = doc.load_page(sticker["page"] - 1)
        w_mm = page.rect.width * 25.4 / 72
        h_mm = page.rect.height * 25.4 / 72
        # Do not shrink an entire A4 product page onto a tiny WB label.
        if not 18 <= w_mm <= 85 or not 18 <= h_mm <= 85:
            raise PilotError(
                f"Размер исходной страницы {w_mm:.1f}×{h_mm:.1f} мм. "
                "Это не отдельная этикетка. Печать запрещена."
            )
        # Scale uniformly to fit 40×30, never crop or distort WB code.
        scale = min(WIDTH_DOTS / page.rect.width, HEIGHT_DOTS / page.rect.height)
        pix = page.get_pixmap(
            matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False
        )
        if pix.width > WIDTH_DOTS or pix.height > HEIGHT_DOTS:
            raise PilotError("Изображение превышает размеры 40×30 мм.")
        packed = pack_monochrome(pix.samples, pix.width, pix.height, pix.stride)
        return {
            "pdf_path": str(path), "pdf_sha256": digest,
            "wb_code": wb_code, "page": sticker["page"],
            "article": product["article"], "source_mm": [round(w_mm, 1), round(h_mm, 1)],
            "raster_sha256": hashlib.sha256(packed).hexdigest(),
        }, packed


def pack_monochrome(samples, width, height, stride, threshold=165):
    """TSPL BITMAP: 0 is dark, 1 is white; white padding is essential."""
    if width < 1 or height < 1 or width > WIDTH_DOTS or height > HEIGHT_DOTS:
        raise PilotError("Недопустимый размер изображения.")
    if len(samples) < stride * height or stride < width:
        raise PilotError("Повреждены растровые данные.")
    out = bytearray(b"\xff" * (BYTES_PER_ROW * HEIGHT_DOTS))
    xoff = (WIDTH_DOTS - width) // 2
    yoff = (HEIGHT_DOTS - height) // 2
    count = 0
    for y in range(height):
        offset = y * stride
        for x in range(width):
            if samples[offset + x] < threshold:
                gx, gy = x + xoff, y + yoff
                out[gy * BYTES_PER_ROW + gx // 8] &= ~(1 << (7 - gx % 8))
                count += 1
    if count < 50 or count > WIDTH_DOTS * HEIGHT_DOTS * 0.85:
        raise PilotError("Стикер выглядит пустым или залит чёрным. Печать запрещена.")
    return bytes(out)


def tspl_bitmap(packed):
    if len(packed) != HEIGHT_DOTS * BYTES_PER_ROW:
        raise PilotError("Некорректный размер TSPL растра.")
    return (
        b"SIZE 40 mm,30 mm\r\nGAP 2 mm,0 mm\r\nDIRECTION 1\r\nCLS\r\n"
        + f"BITMAP 0,0,{BYTES_PER_ROW},{HEIGHT_DOTS},0,".encode("ascii")
        + packed
        + b"\r\nPRINT 1,1\r\n"
    )


def png_from_packed(packed):
    """Show EXACTLY the black/white pixels that will reach TSPL."""
    pixels = bytearray()
    for y in range(HEIGHT_DOTS):
        pixels.append(0)  # PNG grayscale filter None
        for byte in packed[y * BYTES_PER_ROW:(y + 1) * BYTES_PER_ROW]:
            for k in range(7, -1, -1):
                pixels.append(255 if (byte >> k) & 1 else 0)
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH_DOTS, HEIGHT_DOTS,
                                       8, 0, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(bytes(pixels), 9)) +
            chunk(b"IEND", b""))


def paths_for(pdf_path, wb_code):
    folder = Path(pdf_path).resolve().parent
    base = "ONIX_WB_" + wb_code
    return folder / (base + "_PREVIEW.png"), folder / (base + "_REVIEW.json")


def write_review(pdf_path, wb_code):
    meta, packed = read_and_select(pdf_path, wb_code)
    png, journal = paths_for(pdf_path, wb_code)
    if png.exists() or journal.exists():
        raise PilotError(
            "Предпросмотр этого ШК уже существует. "
            "Не создаём повторную печать. Проверь файлы ONIX_WB_ в папке PDF."
        )
    meta["status"] = "preview_ready"
    meta["preview_created_utc"] = datetime.now(timezone.utc).isoformat()
    meta["printer"] = TEST_PRINTER_NAME
    # Both files are local, never sent to ONIX or Render.
    try:
        with journal.open("x", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
        with png.open("xb") as f:
            f.write(png_from_packed(packed))
    except Exception:
        # Review JSON remains as an intentional no-retry safety marker.
        raise
    return png, journal, meta


def _write_status(journal, data, status):
    data = dict(data, status=status)
    tmp = journal.with_suffix(".new")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, journal)


def _spool_one(packed, spooler):
    verify_safe_test_destination(spooler, TEST_PRINTER_NAME)
    handle = None
    payload = tspl_bitmap(packed)
    try:
        handle = spooler.OpenPrinter(TEST_PRINTER_NAME)
        spooler.StartDocPrinter(handle, 1, ("ONIX MANUAL WB PILOT", None, "RAW"))
        try:
            spooler.StartPagePrinter(handle)
            try:
                sent = spooler.WritePrinter(handle, payload)
                if sent != len(payload):
                    raise PilotError("Windows приняла не все байты. Не повторяй печать.")
            finally:
                spooler.EndPagePrinter(handle)
        finally:
            spooler.EndDocPrinter(handle)
    except PilotError:
        raise
    except Exception as exc:
        raise PilotError("Windows не подтвердила отправку. Не повторяй печать.") from exc
    finally:
        if handle is not None:
            spooler.ClosePrinter(handle)


def print_review(pdf_path, wb_code, confirmed_code, spooler=None):
    if confirmed_code != wb_code:
        raise PilotError("Подтверждённый ШК не совпадает с выбранным.")
    png, journal = paths_for(pdf_path, wb_code)
    if not png.is_file() or not journal.is_file():
        raise PilotError("Сначала выполни предпросмотр и проверь изображение.")
    saved = json.loads(journal.read_text(encoding="utf-8"))
    if saved.get("status") != "preview_ready":
        raise PilotError("Эта этикетка уже заявлена к печати. Повтор запрещён.")
    fresh, packed = read_and_select(pdf_path, wb_code)
    for field in ("pdf_path", "pdf_sha256", "wb_code", "page",
                  "article", "source_mm", "raster_sha256", "printer"):
        expected = TEST_PRINTER_NAME if field == "printer" else fresh.get(field)
        if saved.get(field) != expected:
            raise PilotError("Исходный PDF или параметры изменились. Печать запрещена.")
    if png.read_bytes() != png_from_packed(packed):
        raise PilotError("Файл предпросмотра изменён. Печать запрещена.")

    spooler = spooler or _win32print()
    verify_safe_test_destination(spooler, TEST_PRINTER_NAME)
    lock = journal.with_suffix(".lock")
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError as exc:
        raise PilotError("Этот ШК уже отправлялся на печать или требуется сверка.") from exc
    # Claim is durable BEFORE USB send, so a process crash does not cause
    # an accidental second print after restart.
    _write_status(journal, saved, "claimed")
    try:
        _spool_one(packed, spooler)
    except Exception:
        _write_status(journal, saved, "uncertain")
        raise
    _write_status(journal, saved, "submitted")
    return saved


def main(argv=None):
    p = argparse.ArgumentParser(
        description="ONIX: один оригинальный WB-стикер на отдельный USB Xprinter"
    )
    p.add_argument("--pdf", required=True, help="Путь к исходному PDF WB")
    p.add_argument("--barcode", required=True, help="11 цифр оригинального ШК WB")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", action="store_true",
                        help="Создать локальный PNG без печати")
    action.add_argument("--print-one", action="store_true",
                        help="Один раз распечатать ранее проверенный PNG")
    p.add_argument("--confirm-code", help="Повтори 11 цифр WB для разрешения печати")
    args = p.parse_args(argv)
    try:
        if args.preview:
            png, journal, meta = write_review(args.pdf, args.barcode)
            print("Готово БЕЗ ПЕЧАТИ:", png)
            print("Артикул:", meta["article"], "Страница:", meta["page"],
                  "Размер PDF (мм):", meta["source_mm"])
            print("Открой PNG, проверь изображение и только затем разрешай печать.")
        else:
            if not args.confirm_code:
                p.error("Для --print-one обязательно укажи --confirm-code")
            print_review(args.pdf, args.barcode, args.confirm_code)
            print("Отправлен ОДИН оригинальный стикер WB в очередь тестового Xprinter.")
            print("Это не подтверждение физической печати и не готовая FBS-сборка.")
            print("Не используй тестовую этикетку на заказе. Проверь ШК сканером.")
        return 0
    except (PilotError, PrintPilotError, OSError, ValueError) as exc:
        print("ONIX: печать отменена: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
