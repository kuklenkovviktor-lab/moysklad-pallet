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
                return response.status, json.loads(response.read())
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
sku = "SYNTH-SKU-V"
orders = worker("/demo/orders?qr=" + urllib.parse.quote(qr, safe=""))[1]
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

print("PR192 DEMO HTTP SMOKE PASS: manager, printer QR 58x40, line routing,")
print("badge, claim, wrong SKU rejection, scan, complete, release and audit.")
print("SYNTHETIC ONLY; no actual WB orders or physical printing.")
