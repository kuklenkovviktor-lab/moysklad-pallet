#!/usr/bin/env python3
"""End-to-end browser HTTP acceptance against localhost PR192 disposable demo.

Only local demo address; blocks any remote hostname and any non-demo port.
A manager password is passed in via environment, not printed or sent elsewhere.
"""
import http.cookiejar
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

port = os.environ.get("GRIT_PR192_DEMO_PORT", "18765")
if not port.isdecimal() or not 1024 <= int(port) <= 65535:
    raise SystemExit("Invalid localhost-only port")
base = "http://127.0.0.1:" + port
password = os.environ.get("GRIT_PR192_DEMO_PASSWORD", "")
if not password:
    raise SystemExit("Missing synthetic manager password")


def client():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def send(path, data=None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(base + path, data=body,
            headers={"Content-Type": "application/json"} if body else {},
            method="POST" if body is not None else "GET")
        try:
            with opener.open(req, timeout=12) as response:
                return response.status, (response.read().decode('utf-8') if path == '/fbs' else json.loads(response.read()))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())
    return send


manager, worker = client(), client()
assert manager("/health") == (200, {"isolation": "synthetic", "ok": True, "production": False})
assert manager("/demo/login", {"mode": "manager", "password": password})[0] == 200
ms = manager("/demo/session")[1]
assert ms["manager"] and ms["manager_csrf"]
csrf = ms["csrf"]

# Printer for line В and a valid printed 58 x 40 label.
code, printer = manager("/demo/printers", {
    "csrf": csrf, "name": "Изолированный тест: линия В", "lines": ["В"]})
assert code == 201 and "58mm" in printer["label_html"]
qr = printer["qr"]

assert worker("/demo/login", {"mode": "employee", "badge": "DEMO-BADGE-1"})[0] == 200
ws = worker("/demo/session")[1]
assert ws["employee"] == "demo-e1"
# PR209: same existing /fbs screen, not the separate demo worker-qr page.
real_fbs = worker("/fbs")
assert real_fbs[0] == 200 and "async function handleScan(code)" in real_fbs[1], real_fbs[0]
assert "if(code.startsWith('GRIT-PRINTER:'))await acceptPrinterQr(code)" in real_fbs[1]
assert "else if(pendingProduct)await acceptWb(code)" in real_fbs[1]
assert worker("/api/auth/me")[1]["employee"]["id"] == "demo-e1"
current = worker("/api/shift/current")
assert current[0] == 200 and current[1]["active"], current
print("PR209 EXISTING FBS /fbs REAL WORKER QR HTTP SMOKE PASS: no new employee page.")
sku = "SYNTH-SKU-V"
orders = worker("/demo/orders", {"csrf": ws["csrf"], "qr": qr})[1]
assert "SYNTH-1003" in [o["order_id"] for o in orders["orders"]]
# Different-line order cannot be issued to В.
assert "SYNTH-1001" not in [o["order_id"] for o in orders["orders"]]

res = worker("/synthetic/pr192/reserve", {
    "order_id": "SYNTH-1003", "printer_qr": qr,
    "reservation_id": "smoke-lease-3", "action_id": "smoke-reserve-3"})
assert res[0] == 200 and res[1]["state"] == "RESERVED", res
wrong = worker("/demo/scan", {"csrf": ws["csrf"], "order_id": "SYNTH-1003",
    "reservation_id": "smoke-lease-3", "sku": "SYNTH-INVALID"})
assert wrong[0] == 409, wrong
scan = worker("/demo/scan", {"csrf": ws["csrf"], "order_id": "SYNTH-1003",
    "reservation_id": "smoke-lease-3", "sku": sku})
assert scan[0] == 200, scan
complete = worker("/synthetic/pr192/complete", {
    "order_id": "SYNTH-1003", "reservation_id": "smoke-lease-3",
    "action_id": "smoke-complete-3"})
assert complete[0] == 200 and complete[1]["state"] == "COMPLETED", complete
audit = manager("/synthetic/pr192/audit/SYNTH-1003")
assert audit[0] == 200 and [e["action"] for e in audit[1]["events"]] == ["RESERVE", "COMPLETE"]

# Revoking a printer QR invalidates the original immediately.
rotated = manager("/demo/printers/" + printer["printer_id"] + "/rotate",
                  {"csrf": csrf})
assert rotated[0] == 200, rotated
assert rotated[1]["qr"] != qr
assert worker("/demo/orders", {"csrf": ws["csrf"], "qr": qr})[0] == 403
configured = manager("/demo/printers/" + printer["printer_id"] + "/configure",
                     {"csrf": csrf, "name": "Тест В-Б", "lines": ["В", "Б"]})
assert configured[0] == 200, configured

# A second order on line А is explicitly released by the manager.
code, p2 = manager("/demo/printers", {
    "csrf": csrf, "name": "Изолированный тест: линия А", "lines": ["А"]})
assert code == 201
res = worker("/synthetic/pr192/reserve", {
    "order_id": "SYNTH-1001", "printer_qr": p2["qr"],
    "reservation_id": "smoke-lease-1", "action_id": "smoke-reserve-1"})
assert res[0] == 200, res
released = manager("/synthetic/pr192/release", {
    "order_id": "SYNTH-1001", "reservation_id": "smoke-lease-1",
    "action_id": "smoke-release-1",
    "reason": "Синтетическая проверка освобождения",
    "csrf": ms["manager_csrf"]})
assert released[0] == 200 and released[1]["state"] == "RELEASED", released
assert manager("/synthetic/pr192/status/SYNTH-1001")[1]["state"] == "RELEASED"
assert [e["action"] for e in manager("/synthetic/pr192/audit/SYNTH-1001")[1]["events"]] == ["RESERVE", "RELEASE"]
assert worker("/synthetic/pr192/release", {
    "order_id": "SYNTH-1001", "reservation_id": "smoke-lease-1",
    "reason": "Unauthorized", "action_id": "bad-release",
    "csrf": ms["manager_csrf"]})[0] == 403

# New FSD scanner integration: server-issued session CSRF and QR-only enqueue.
scanner_session = worker("/synthetic/pr192/print-queue/session")
assert scanner_session[0] == 200 and scanner_session[1]["synthetic"], scanner_session
assert manager("/synthetic/pr192/print-queue/session")[0] == 401
virtual_job = worker("/synthetic/pr192/print-queue/scan", {
    "qr": p2["qr"], "csrf": scanner_session[1]["csrf"]})
assert virtual_job[0] == 200 and virtual_job[1]["queued_new"] == 1, virtual_job
assert virtual_job[1]["physical_printing"] is False
rescan = worker("/synthetic/pr192/print-queue/scan", {
    "qr": p2["qr"], "csrf": scanner_session[1]["csrf"]})
assert rescan[0] == 200 and rescan[1]["already_queued"] == 1, rescan
print("PR192 FSD WORKER SCANNER QR HTTP SMOKE PASS: WMS session, no order selection.")

# Manager-only queue visibility and explicit one-job VIRTUAL sink acceptance.
snapshot=manager("/demo/printing/state")
assert snapshot[0]==200 and snapshot[1]["synthetic"],snapshot
assert snapshot[1]["physical_printing"] is False
assert snapshot[1]["summary"]["QUEUED"] == 1, snapshot
assert snapshot[1]["summary"]["total"] == 1, snapshot
printer_view = next(p for p in snapshot[1]["printers"] if p["id"] == p2["printer_id"])
assert printer_view["name"] == "Изолированный тест: линия А", printer_view
assert printer_view["queued"] == 1 and printer_view["last_state"] == "QUEUED", printer_view
assert any(j["order_id"]=="SYNTH-1001" and j["state"]=="QUEUED"
           for j in snapshot[1]["jobs"]),snapshot
assert worker("/demo/printing/state")[0]==403
denied=worker("/demo/printing/virtual-deliver",
              {"printer_id":p2["printer_id"],"csrf":ms["manager_csrf"]})
assert denied[0]==403,denied
acked=manager("/demo/printing/virtual-deliver",{
    "printer_id":p2["printer_id"],"csrf":ms["manager_csrf"]})
assert acked[0]==200 and acked[1]["state"]=="SUBMITTED",acked
assert acked[1]["physical_printing"] is False
after=manager("/demo/printing/state")
assert after[1]["summary"]["SUBMITTED"] == 1, after
assert after[1]["summary"]["QUEUED"] == 0, after
assert any(p["id"] == p2["printer_id"] and p["last_state"] == "SUBMITTED"
           for p in after[1]["printers"]), after
assert after[0]==200 and any(
    j["order_id"]=="SYNTH-1001" and j["state"]=="SUBMITTED"
    for j in after[1]["jobs"]),after
again=manager("/demo/printing/virtual-deliver",{
    "printer_id":p2["printer_id"],"csrf":ms["manager_csrf"]})
assert again[0]==200 and again[1]["state"]=="EMPTY",again
print("PR192 MANAGER VIRTUAL PRINTING HTTP SMOKE PASS: queued -> submitted; no USB.")
print("PR192 COMMERCIAL PRINTING CONSOLE HTTP SMOKE PASS: names, counts, statuses.")

print("PR192 DEMO HTTP SMOKE PASS: manager, printer QR 58x40, line routing,")
print("badge, claim, wrong SKU rejection, scan, complete, release and audit.")
print("SYNTHETIC ONLY; no actual WB orders or physical printing.")
