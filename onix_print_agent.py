"""One-shot ONIX cloud -> Windows TEST printer agent. No WB prints.

This opt-in command makes exactly one request for a diagnostic job, sends
one hard-coded local TSPL test label to the named nondefault queue, and
reports 'submitted to Windows spooler' (NOT physically confirmed).
Claimed jobs are NEVER automatically retried.
"""
import argparse
import getpass
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPSHandler, HTTPRedirectHandler

from onix_usb_print_test import (
    TEST_PRINTER_NAME, PrintPilotError, _win32print,
    submit_one_test_label, verify_safe_test_destination,
)

DEFAULT_SERVER = "https://onix-wms-frankfurt.onrender.com"
ALLOWED_SERVERS = {DEFAULT_SERVER}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PrintAgentError(RuntimeError):
    pass


def _http(server, token, route, method="GET", payload=None):
    if server not in ALLOWED_SERVERS:
        raise PrintAgentError("Доступ разрешён только к официальному ONIX Frankfurt (HTTPS).")
    body=None if payload is None else json.dumps(payload).encode("utf-8")
    headers={"Authorization":"Bearer "+token,"Accept":"application/json"}
    if body is not None:
        headers["Content-Type"]="application/json"
    req=Request(server+route, data=body, method=method, headers=headers)
    opener=build_opener(HTTPSHandler(context=ssl.create_default_context()),NoRedirect())
    try:
        with opener.open(req,timeout=20) as response:
            if response.status==204:
                return None
            if response.status!=200:
                raise PrintAgentError("Неожиданный ответ ONIX: "+str(response.status))
            raw=response.read(4096)
            return json.loads(raw)
    except HTTPError as exc:
        if exc.code==401:
            raise PrintAgentError("ONIX отклонил ключ агента. Проверь Render и локальный ключ.") from exc
        raise PrintAgentError("Ошибка ONIX HTTP "+str(exc.code)) from exc
    except (URLError,TimeoutError,OSError,ValueError) as exc:
        raise PrintAgentError("Нет надёжного соединения с ONIX или неверный ответ сервера.") from exc


def run_once(server, token, spooler=None):
    """At most one claim, at most one write, never a retry."""
    if len(token)<32:
        raise PrintAgentError("Ключ ONIX_PRINT_AGENT_TOKEN должен содержать минимум 32 символа.")
    # Verify printer BEFORE claiming a server job, so a default-printer mistake
    # cannot put the job in a permanently claimed state.
    spooler=spooler or _win32print()
    verify_safe_test_destination(spooler, TEST_PRINTER_NAME)

    job=_http(server,token,"/api/print-agent/test/next")
    if job is None:
        print("Тестовых заданий в ONIX нет. Ничего не напечатано.")
        return
    if not isinstance(job,dict):
        raise PrintAgentError("ONIX вернул некорректное задание. Печать запрещена.")
    job_id=job.get("job_id","")
    if (job.get("kind")!="onix-test-only"
            or job.get("printer")!=TEST_PRINTER_NAME
            or not isinstance(job_id,str) or len(job_id)!=36):
        raise PrintAgentError("Некорректный тип задания. Печать запрещена.")

    outcome="uncertain"
    try:
        submit_one_test_label(spooler,TEST_PRINTER_NAME)
        outcome="submitted"
    except PrintPilotError:
        print("Windows не подтвердила постановку теста в очередь. Без автоматического повтора.")
    finally:
        try:
            _http(server,token,"/api/print-agent/test/ack",method="POST",
                  payload={"job_id":job_id,"status":outcome})
        except PrintAgentError as exc:
            # No retry; operator must inspect ONIX and Windows manually.
            print("ВНИМАНИЕ: не удалось подтвердить статус в ONIX: "+str(exc))
            print("НЕ ПОВТОРЯЙ печать автоматически. Проверь очередь Windows.")
            raise
    if outcome=="submitted":
        print("Одно тестовое задание передано в очередь Windows.")
        print("Физическую печать проверь на ONIX-TEST-XP460B. Это не этикетка WB.")
    else:
        raise PrintAgentError("Тест не подтверждён. Проверь очередь и статус ONIX вручную.")


def main(argv=None):
    parser=argparse.ArgumentParser(description="ONIX agent: one cloud diagnostic label")
    parser.add_argument("--once",action="store_true",
                        help="Забрать максимум одно тестовое задание, не выполнять автоповторы")
    args=parser.parse_args(argv)
    if not args.once:
        parser.error("Используй --once (фоновый режим специально отключён)")
    token=os.environ.get("ONIX_PRINT_AGENT_TOKEN","").strip()
    if not token:
        token=getpass.getpass("Введи ONIX_PRINT_AGENT_TOKEN (скрытый ввод): ").strip()
    try:
        run_once(DEFAULT_SERVER,token)
    except (PrintAgentError,PrintPilotError) as exc:
        print("ONIX агент остановлен: "+str(exc))
        return 1
    return 0


if __name__=="__main__":
    raise SystemExit(main())
