"""GRIT WMS: ONE manual 58x40-mm synthetic label on an isolated Windows USB Xprinter.

Run locally on a permitted test PC. Never reads GRIT/WB/API credentials, jobs,
order PDFs or customer data. Never connects to the network or runs a service.
The only printer allowed is a dedicated, NON-default ONIX-TEST-XP460B
installed on a direct local USB port. Explicit confirmation and a durable
single-attempt journal prevent accidental retry after uncertain spooler ACK.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys


TEST_PRINTER = "ONIX-TEST-XP460B"
CONFIRM = "PRINT-ONE-GRIT-58X40"
TEST_QR = "GRIT-USB-TEST-ONLY"
TEST_DOC = "GRIT WMS TEST 58x40 - NOT WB"
JOURNAL_FILENAME = "grit_xprinter_58x40_one_attempt.json"


class PilotError(RuntimeError):
    """Local, non-production printing safety guard failed."""


def build_test_label():
    """Exactly one fixed TSPL diagnostic label; no WB/GRIT printer routing code."""
    lines = (
        "SIZE 58 mm,40 mm",
        "GAP 2 mm,0 mm",
        "DIRECTION 1",
        "CLS",
        'BOX 12,12,452,308,2',
        'TEXT 20,24,"3",0,1,1,"GRIT WMS"',
        'TEXT 20,58,"2",0,1,1,"SYNTHETIC"',
        'TEXT 20,88,"2",0,1,1,"NOT WB"',
        'TEXT 20,123,"1",0,1,1,"58x40 mm"',
        'TEXT 20,268,"2",0,1,1,"USB TEST 1/1"',
        'QRCODE 240,42,L,6,A,0,"GRIT-USB-TEST-ONLY"',
        "PRINT 1,1",
    )
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def _spooler():
    if sys.platform != "win32":
        raise PilotError("Нужен Windows и отдельный USB-принтер.")
    try:
        import win32print
    except ImportError as exc:
        raise PilotError("Не установлен pywin32: py -m pip install pywin32") from exc
    return win32print


def _installed_names(spooler):
    rows = spooler.EnumPrinters(
        spooler.PRINTER_ENUM_LOCAL | spooler.PRINTER_ENUM_CONNECTIONS,
        None, 2,
    )
    return sorted(str(row["pPrinterName"]) for row in rows)


def check_printer(spooler, name):
    """Never choose the system default; reject non-local or non-USB ports."""
    if name != TEST_PRINTER:
        raise PilotError("Разрешён только выделенный тестовый ONIX-TEST-XP460B.")
    if name not in _installed_names(spooler):
        raise PilotError("Тестовая очередь Windows не обнаружена. Печать запрещена.")
    try:
        default = spooler.GetDefaultPrinter()
    except Exception as exc:
        raise PilotError("Не удалось проверить принтер по умолчанию.") from exc
    if default == TEST_PRINTER:
        raise PilotError(
            "Тестовый принтер является принтером Windows по умолчанию. "
            "Нужна отдельная тестовая очередь, не используемая для рабочих заданий."
        )
    handle = None
    try:
        handle = spooler.OpenPrinter(name)
        data = spooler.GetPrinter(handle, 2)
        port = str(data.get("pPortName", "")).strip()
    except Exception as exc:
        raise PilotError("Не удалось проверить USB-порт выделенного принтера.") from exc
    finally:
        if handle is not None:
            spooler.ClosePrinter(handle)
    if not re.fullmatch(r"USB[0-9]{1,4}", port, flags=re.IGNORECASE):
        raise PilotError(
            "Принтер не привязан к отдельному USB-порту Windows (USB001 и т.п.): "
            "физическая отправка запрещена."
        )
    return port


def _journal_file(state_dir=None):
    if state_dir is None:
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise PilotError("LOCALAPPDATA не обнаружен: журнал блокировки недоступен.")
        state_dir = Path(base) / "GRIT-WMS" / "USB-Pilot"
    return Path(state_dir) / JOURNAL_FILENAME


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _write_once(path, record):
    """Create the do-not-repeat marker BEFORE the first spooler operation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise PilotError(
            "Попытка уже зарегистрирована. Повторная печать запрещена. "
            "Сверь бумагу, очередь Windows и локальный журнал."
        ) from exc
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def _update_journal(path, record, state, **extras):
    new = dict(record, state=state, updated_utc=_utc_now(), **extras)
    staging = path.with_name(path.name + ".new")
    with staging.open("x", encoding="utf-8") as out:
        json.dump(new, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(staging, path)


def _send_exactly_one(spooler, printer_name, payload):
    """No retry, no fallback. spooler ACK is NOT physical print evidence."""
    handle = None
    doc_started = False
    page_started = False
    try:
        handle = spooler.OpenPrinter(printer_name)
        spool_job_id = spooler.StartDocPrinter(
            handle, 1, (TEST_DOC, None, "RAW")
        )
        doc_started = True
        spooler.StartPagePrinter(handle)
        page_started = True
        written = spooler.WritePrinter(handle, payload)
        if written != len(payload):
            raise PilotError(
                "Windows не приняла все байты. Исход печати неизвестен, не повторяй."
            )
        spooler.EndPagePrinter(handle)
        page_started = False
        spooler.EndDocPrinter(handle)
        doc_started = False
        return spool_job_id
    except PilotError:
        raise
    except Exception as exc:
        raise PilotError(
            "Ошибка драйвера/USB при отправке. Исход неизвестен. "
            "Автоповтор запрещён."
        ) from exc
    finally:
        if handle is not None:
            if page_started:
                try:
                    spooler.EndPagePrinter(handle)
                except Exception:
                    pass
            if doc_started:
                try:
                    spooler.EndDocPrinter(handle)
                except Exception:
                    pass
            spooler.ClosePrinter(handle)


def print_one(spooler, printer_name, *, state_dir=None):
    port = check_printer(spooler, printer_name)
    payload = build_test_label()
    path = _journal_file(state_dir)
    record = {
        "phase": "GRIT-USB-58x40-SYNTHETIC",
        "printer": TEST_PRINTER,
        "usb_port": port,
        "kind": "TEST-ONLY-NO-WB",
        "qr": TEST_QR,
        "label_mm": [58, 40],
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "state": "CLAIMED",
        "created_utc": _utc_now(),
        "physical_print_confirmed": False,
    }
    _write_once(path, record)
    try:
        spool_job_id = _send_exactly_one(spooler, printer_name, payload)
    except Exception:
        # Marker remains even if writing this status fails: do NOT resend.
        try:
            _update_journal(path, record, "UNKNOWN")
        except Exception:
            pass
        raise
    try:
        _update_journal(path, record, "SUBMITTED_TO_WINDOWS",
                        spool_job_id=spool_job_id)
    except Exception as exc:
        raise PilotError(
            "Задание передано, но запись статуса не завершилась. "
            "Повторять печать запрещено."
        ) from exc
    return path


def main(argv=None, *, spooler=None, state_dir=None):
    parser = argparse.ArgumentParser(
        description="GRIT WMS: однократная синтетическая проверка USB Xprinter 58x40."
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--preview", action="store_true",
                        help="Проверить шаблон TSPL, не подключаясь к принтеру")
    action.add_argument("--list", action="store_true",
                        help="Перечислить принтеры Windows, ничего не печатая")
    action.add_argument("--check", action="store_true",
                        help="Проверить отдельную USB-очередь, ничего не печатая")
    action.add_argument("--status", action="store_true",
                        help="Показать локальный журнал, ничего не печатая")
    action.add_argument("--print-one", action="store_true",
                        help="Единожды отправить 58x40 TEST-ONLY этикетку")
    parser.add_argument("--printer", help="Строго ONIX-TEST-XP460B")
    parser.add_argument("--confirm",
                        help="Только для физической печати: PRINT-ONE-GRIT-58X40")
    args = parser.parse_args(argv)

    try:
        if args.preview:
            raw = build_test_label()
            print("GRIT SYNTHETIC 58x40 / NOT WB / NO PRINT")
            print("SHA256:", hashlib.sha256(raw).hexdigest())
            print(raw.decode("ascii"))
            return 0

        if args.status:
            path = _journal_file(state_dir)
            if not path.exists():
                print("Журнала нет: физическая попытка не зарегистрирована.")
                return 0
            print(path.read_text(encoding="utf-8"))
            return 0

        if args.print_one:
            if args.confirm != CONFIRM:
                raise PilotError("Требуется точное подтверждение --confirm " + CONFIRM)
            if args.printer != TEST_PRINTER:
                raise PilotError("Нужно явно указать --printer " + TEST_PRINTER)

        if args.check and args.printer != TEST_PRINTER:
            raise PilotError("Для проверки укажи --printer " + TEST_PRINTER)

        device = spooler if spooler is not None else _spooler()
        if args.list:
            default = device.GetDefaultPrinter()
            for printer in _installed_names(device):
                marker = " (по умолчанию)" if printer == default else ""
                print(printer + marker)
            return 0
        if args.check:
            port = check_printer(device, args.printer)
            print("USB CHECK PASS:", args.printer, "port:", port)
            print("Это сухая проверка. Задания не отправлялись.")
            return 0
        journal = print_one(device, args.printer, state_dir=state_dir)
        print("SUBMITTED_TO_WINDOWS: одна тестовая этикетка 58x40.")
        print("Журнал:", journal)
        print("ФИЗИЧЕСКИЙ ОТПЕЧАТОК НЕ ПОДТВЕРЖДЁН. Проверь бумагу и QR вручную.")
        print("НЕ ПОВТОРЯЙ команду, если результата нет или он неясен.")
        return 0
    except (PilotError, OSError, ValueError) as exc:
        print("GRIT USB TEST REFUSED: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
