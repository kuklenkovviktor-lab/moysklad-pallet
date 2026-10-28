"""ONIX WB queue agent dry-run. NEVER submits a physical print.

Reads one queued job without claiming it, validates the original LOCAL WB PDF,
the SHA256, exact sticker, article and source page, then exits.
"""
import argparse
import getpass
import hashlib
import json
import os
import ssl
from pathlib import Path
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler
from urllib.error import HTTPError, URLError

from onix_wb_label_pilot import read_and_select, PilotError
from onix_usb_print_test import TEST_PRINTER_NAME, _win32print, verify_safe_test_destination

SERVER = "https://onix-wms-frankfurt.onrender.com"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def preview_job(token):
    if len(token) < 32:
        raise ValueError("Ключ агента слишком короткий")
    req = Request(SERVER + "/api/print-agent/wb/preview-next",
                  headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
    opener = build_opener(HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
    try:
        with opener.open(req, timeout=20) as response:
            if response.status == 204:
                return None
            if response.status != 200:
                raise ValueError("Неожиданный статус ответа ONIX")
            return json.loads(response.read(4096))
    except (HTTPError, URLError) as exc:
        raise ValueError("Не удалось безопасно получить задание ONIX") from exc


def validate_job(job, pdf_path, spooler):
    if not isinstance(job, dict) or job.get("mode") != "dry_run_only":
        raise ValueError("Недопустимый режим задания")
    if job.get("printer") != TEST_PRINTER_NAME:
        raise ValueError("Рабочий принтер запрещён")
    verify_safe_test_destination(spooler, TEST_PRINTER_NAME)
    path = Path(pdf_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Нужен оригинальный локальный WB PDF")
    digest = hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
    if digest != job.get("pdf_sha256"):
        raise ValueError("SHA256 не совпадает: это другой PDF")
    meta, packed = read_and_select(path, str(job.get("wb_code","")))
    if meta["article"] != job.get("article") or meta["page"] != job.get("page_no"):
        raise ValueError("ШК, артикул или страница не совпадают")
    return {"wb_code":meta["wb_code"],"article":meta["article"],
            "page":meta["page"],"printer":TEST_PRINTER_NAME,
            "target_mm":meta["target_mm"],"verified":True,
            "printing_enabled":False}


def main(argv=None):
    p=argparse.ArgumentParser(description="Проверка WB-задания ONIX, без печати")
    p.add_argument("--dry-run",action="store_true",required=True)
    p.add_argument("--pdf",required=True,help="Оригинальный WB PDF из той же партии")
    args=p.parse_args(argv)
    try:
        token=os.environ.get("ONIX_PRINT_AGENT_TOKEN","").strip()
        if not token:
            token=getpass.getpass("Ключ ONIX (ввод скрыт): ").strip()
        job=preview_job(token)
        if job is None:
            print("В очереди нет заданий. Печать не выполнялась.")
            return 0
        result=validate_job(job,args.pdf,_win32print())
        print(json.dumps(result,ensure_ascii=False,indent=2))
        print("Проверка завершена. НИ ОДНОГО задания печати не отправлено.")
        return 0
    except (ValueError,PilotError,OSError) as exc:
        print("ONIX dry-run отменён:",str(exc))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
