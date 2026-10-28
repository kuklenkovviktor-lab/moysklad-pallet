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

from onix_wb_label_pilot import read_and_select, PilotError, _spool_one
from onix_usb_print_test import TEST_PRINTER_NAME, _win32print, verify_safe_test_destination

SERVER = "https://onix-wms-frankfurt.onrender.com"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def remote(token, route, method="GET", data=None):
    if len(token) < 32:
        raise ValueError("Ключ агента слишком короткий")
    payload = None if data is None else json.dumps(data).encode("utf-8")
    req = Request(SERVER + route,method=method,data=payload,
                  headers={"Authorization":"Bearer "+token,
                           "Accept":"application/json",
                           "Content-Type":"application/json"})
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
    if not isinstance(job, dict) or job.get("mode") not in ("dry_run_only","claim_no_auto_retry"):
        raise ValueError("Недопустимый режим задания")
    if job.get("printer") != TEST_PRINTER_NAME:
        raise ValueError("Рабочий принтер запрещён")
    verify_safe_test_destination(spooler, TEST_PRINTER_NAME)
    path = Path(pdf_path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".pdf":
        raise ValueError("Нужен оригинальный локальный WB PDF")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != job.get("pdf_sha256"):
        raise ValueError("SHA256 не совпадает: это другой PDF")
    meta, packed = read_and_select(path, str(job.get("wb_code","")))
    if meta["article"] != job.get("article") or meta["page"] != job.get("page_no"):
        raise ValueError("ШК, артикул или страница не совпадают")
    return {"wb_code":meta["wb_code"],"article":meta["article"],
            "page":meta["page"],"printer":TEST_PRINTER_NAME,
            "target_mm":meta["target_mm"],"verified":True,
            "printing_enabled":False}


def do_print_one(token, pdf_path, spooler):
    # Preflight before claiming. Never spool without exact validated claim.
    queued=remote(token,"/api/print-agent/wb/preview-next")
    if not queued:
        print("Нет заданий.")
        return
    validate_job(queued,pdf_path,spooler)
    claimed=remote(token,"/api/print-agent/wb/claim-one",method="POST")
    if not claimed:
        raise ValueError("Задание изменилось до закрепления; печать отменена.")
    job_id=claimed.get("job_id")
    if claimed.get("mode")!="claim_no_auto_retry" or not isinstance(job_id,str):
        raise ValueError("Некорректное подтверждение ONIX; печать запрещена.")
    # Claim may be consumed, but this is intentionally not automatically retried.
    status="uncertain"
    try:
        if any(claimed.get(k)!=queued.get(k) for k in
               ("job_id","batch_id","wb_code","article","page_no","printer","pdf_sha256")):
            raise ValueError("Очередь изменилась; не печатаем неподтверждённое задание.")
        meta,packed=read_and_select(pdf_path,claimed["wb_code"])
        if meta["article"]!=claimed["article"] or meta["page"]!=claimed["page_no"]:
            raise ValueError("Артикул или страница изменились.")
        lock=Path(pdf_path).resolve().parent/("ONIX_CLOUD_"+job_id+".lock")
        fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        try:
            os.write(fd,(job_id+" "+claimed["wb_code"]).encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        # One actual RAW spool submission, only to named non-default test printer.
        _spool_one(packed,spooler)
        status="submitted"
    finally:
        try:
            result=remote(token,"/api/print-agent/wb/ack",method="POST",
                          data={"job_id":job_id,"status":status})
            print("ONIX:",result)
        except Exception as exc:
            print("ОШИБКА ACK: Проверь очередь ONIX и Windows вручную. НЕ ПОВТОРЯЙ:",str(exc))
            raise
    print("Передано одно WB-задание в очередь Windows; физический выход не подтверждён.")


def main(argv=None):
    p=argparse.ArgumentParser(description="Проверка WB-задания ONIX, без печати")
    mode=p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run",action="store_true")
    mode.add_argument("--print-one-cloud",action="store_true")
    p.add_argument("--confirm-test-printer",default="",help="Для печати повтори ONIX-TEST-XP460B")
    p.add_argument("--pdf",required=True,help="Оригинальный WB PDF из той же партии")
    args=p.parse_args(argv)
    try:
        token=os.environ.get("ONIX_PRINT_AGENT_TOKEN","").strip()
        if not token:
            token=getpass.getpass("Ключ ONIX (ввод скрыт): ").strip()
        spooler=_win32print()
        if args.print_one_cloud:
            if args.confirm_test_printer!=TEST_PRINTER_NAME:
                raise ValueError("Для облачной печати явно подтверди тестовый принтер")
            do_print_one(token,args.pdf,spooler)
        else:
            job=remote(token,"/api/print-agent/wb/preview-next")
            if job is None:
                print("В очереди нет заданий. Печать не выполнялась.")
                return 0
            result=validate_job(job,args.pdf,spooler)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            print("Проверка завершена. Печать не выполнялась.")
        return 0
    except (ValueError,PilotError,OSError,FileExistsError) as exc:
        print("ONIX dry-run отменён:",str(exc))
        return 1


if __name__=="__main__":
    raise SystemExit(main())
