"""ONIX Windows USB printer pilot. Test label only; never prints WB order stickers.

Safety:
- Explicit printer name ONIX-TEST-XP460B required.
- Refuses Windows' default printer, guarding normal FBS printing.
- No connection to ONIX/Render, no stock changes and no background polling.
- Exactly one inert TSPL diagnostic label per explicit --send-one-test call.
"""
import argparse
import sys


TEST_PRINTER_NAME = "ONIX-TEST-XP460B"


class PrintPilotError(Exception):
    """Safe pilot cannot proceed."""


def _win32print():
    if sys.platform != "win32":
        raise PrintPilotError("Печать доступна только на Windows.")
    try:
        import win32print
    except ImportError as exc:
        raise PrintPilotError(
            "Не установлен модуль pywin32. Выполни: py -m pip install pywin32"
        ) from exc
    return win32print


def installed_printers(spooler):
    """Enumerate installed Windows queues, not USB ports."""
    flags = spooler.PRINTER_ENUM_LOCAL | spooler.PRINTER_ENUM_CONNECTIONS
    records = spooler.EnumPrinters(flags, None, 2)
    return sorted({entry["pPrinterName"] for entry in records})


def verify_safe_test_destination(spooler, printer_name):
    if printer_name != TEST_PRINTER_NAME:
        raise PrintPilotError(
            "Разрешён только отдельный тестовый принтер с именем "
            + TEST_PRINTER_NAME + ". Рабочий FBS-принтер использовать нельзя."
        )
    if printer_name not in installed_printers(spooler):
        raise PrintPilotError(
            "Тестовый принтер не найден в Windows. "
            "Проверь USB, драйвер и имя устройства."
        )
    try:
        current_default = spooler.GetDefaultPrinter()
    except Exception as exc:
        raise PrintPilotError(
            "Не удалось проверить принтер по умолчанию. Печать отменена."
        ) from exc
    if current_default == printer_name:
        raise PrintPilotError(
            "ONIX-TEST-XP460B назначен принтером по умолчанию. "
            "Поставь рабочий FBS-принтер по умолчанию и повтори тест."
        )


def test_label_tspl():
    """One 40x30 mm label containing NO real Wildberries barcode."""
    lines = [
        "SIZE 40 mm,30 mm",
        "GAP 2 mm,0 mm",
        "DIRECTION 1",
        "CLS",
        'TEXT 14,18,"3",0,1,1,"ONIX TEST"',
        'TEXT 14,60,"2",0,1,1,"NOT WB"',
        'QRCODE 14,98,L,3,A,0,"ONIX-TEST-ONLY"',
        "PRINT 1,1",
    ]
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def submit_one_test_label(spooler, printer_name):
    """Send one TSPL job through the Windows RAW spooler, without retry."""
    verify_safe_test_destination(spooler, printer_name)
    raw = test_label_tspl()
    handle = None
    try:
        handle = spooler.OpenPrinter(printer_name)
        spooler.StartDocPrinter(handle, 1, ("ONIX TEST - NOT WB", None, "RAW"))
        try:
            spooler.StartPagePrinter(handle)
            try:
                sent = spooler.WritePrinter(handle, raw)
                if sent != len(raw):
                    raise PrintPilotError(
                        "Windows приняла не все байты тестового задания. "
                        "Не повторяй печать автоматически."
                    )
            finally:
                spooler.EndPagePrinter(handle)
        finally:
            spooler.EndDocPrinter(handle)
    except PrintPilotError:
        raise
    except Exception as exc:
        raise PrintPilotError(
            "Не удалось отправить тест на выделенный принтер. "
            "Проверь устройство и очередь Windows."
        ) from exc
    finally:
        if handle is not None:
            spooler.ClosePrinter(handle)


def main(argv=None, spooler=None):
    parser = argparse.ArgumentParser(
        description="ONIX: безопасная проверка второго Xprinter XP-460B по USB"
    )
    parser.add_argument("--list", action="store_true",
                        help="Показать принтеры Windows и принтер по умолчанию")
    parser.add_argument("--printer", help="Имя тестового принтера в Windows")
    parser.add_argument("--send-one-test", action="store_true",
                        help="Отправить одну тестовую этикетку 40x30 мм (не WB)")
    args = parser.parse_args(argv)
    if not args.list and not args.printer:
        parser.error("Укажи --list или --printer ONIX-TEST-XP460B")
    if args.send_one_test and not args.printer:
        parser.error("Для печати нужен --printer ONIX-TEST-XP460B")
    spooler = spooler or _win32print()
    try:
        if args.list:
            for name in installed_printers(spooler):
                marker = " (по умолчанию)" if name == spooler.GetDefaultPrinter() else ""
                print(name + marker)
        if args.printer:
            verify_safe_test_destination(spooler, args.printer)
            if args.send_one_test:
                submit_one_test_label(spooler, args.printer)
                print("Одна тестовая этикетка отправлена в очередь Windows.")
                print("Проверь фактическую печать: 40x30 мм, ONIX TEST / NOT WB.")
            else:
                print("Тестовый принтер найден, он не выбран по умолчанию.")
                print("Это сухая проверка: задания печати не отправлялись.")
    except PrintPilotError as exc:
        print("Печать отменена: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
