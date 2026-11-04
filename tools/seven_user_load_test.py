#!/usr/bin/env python3
"""Isolated GRIT WMS concurrent session test. NEVER target production.

Requirements: pip install requests
Usage:
  python tools/seven_user_load_test.py --config tools/seven_user_config.example.json
  python tools/seven_user_load_test.py --config private-staging-config.json --execute --operations 10

A dedicated staging deployment, isolated database, disabled integrations, and seven
test employees MUST be prepared first. Default mode performs NO HTTP requests.
"""
import argparse
import concurrent.futures
import json
import statistics
import threading
import time
from urllib.parse import urlparse

import requests

DANGEROUS_HOSTS = {
    "onix-wms-frankfurt.onrender.com",
    "moysklad-pallet.onrender.com",
    "onix-partner-pallets-test.onrender.com",
}
ROUTES = {
    "picking": "/api/fbs/stats",
    "receiving": "/api/receiving/overview",
    "replenishment": "/api/replenishment/tasks",
    "manager": "/api/stats/employees",
}

def safe_target(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host in DANGEROUS_HOSTS or not host:
        raise ValueError("Production or missing target is forbidden")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path.strip("/"):
        raise ValueError("Use a clean root URL, without credentials, path or query")
    is_local = host in ("localhost", "127.0.0.1")
    is_staging = "staging" in host or "loadtest" in host or host.startswith("test-")
    if not (is_local or (parsed.scheme == "https" and is_staging)):
        raise ValueError("Only localhost or an explicitly named staging/loadtest HTTPS host is allowed")
    return url.rstrip("/")

def run_actor(root, actor, operations, barrier, timeout):
    role = actor["role"]
    name = actor["name"]
    elapsed_ms = []
    unexpected = []
    codes = {}
    with requests.Session() as s:
        barrier.wait(timeout=30)
        def req(method, path, payload=None, expected=(200,)):
            start = time.perf_counter()
            try:
                response = s.request(method, root + path, json=payload, timeout=timeout)
                elapsed_ms.append((time.perf_counter() - start) * 1000)
                codes[str(response.status_code)] = codes.get(str(response.status_code), 0) + 1
                if response.status_code not in expected:
                    unexpected.append(f"{path} returned {response.status_code}: {response.text[:120]}")
                try:
                    return response.json() if response.ok else {}
                except ValueError:
                    unexpected.append(f"{path} returned non-JSON")
                    return {}
            except requests.RequestException as exc:
                elapsed_ms.append((time.perf_counter() - start) * 1000)
                unexpected.append(f"{path}: {type(exc).__name__}: {exc}")
                return {}

        login = req("POST", "/api/auth/login", {"badge_code": actor["badge"]})
        if not login.get("ok"):
            return {"name": name, "role": role, "ok": False, "statuses": codes, "errors": unexpected + ["login failed"], "latency_ms": elapsed_ms}
        if role == "picking":
            started = req("POST", "/api/fbs/start", {})
            shift_id = started.get("shift_id")
            if not shift_id:
                unexpected.append("no shift_id")
            else:
                scans = actor.get("scans", [])
                if len(scans) < operations:
                    unexpected.append("insufficient unique synthetic scans in actor configuration")
                else:
                    for item in scans[:operations]:
                        req("POST", "/api/fbs/scan", {
                            "shift_id": shift_id,
                            "barcode": item["wb_barcode"],
                            "product_barcode": item["product_barcode"],
                            "article": item.get("article", "GRIT-TEST"),
                            "product_name": "ISOLATED_LOAD_TEST",
                            "scan_type": "normal"
                        })
        else:
            path = ROUTES[role]
            for _ in range(operations):
                req("GET", path)
        return {"name": name, "role": role, "ok": not unexpected, "statuses": codes,
                "errors": unexpected, "latency_ms": elapsed_ms}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--operations", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=10)
    args = ap.parse_args()
    if not (1 <= args.operations <= 100):
        ap.error("operations must be 1..100")
    with open(args.config, encoding="utf-8") as f:
        cfg = json.load(f)
    root = safe_target(cfg["base_url"])
    actors = cfg["actors"]
    if len(actors) != 7 or sorted(a["role"] for a in actors) != sorted(["picking"]*4+["receiving","replenishment","manager"]):
        ap.error("Require exactly four pickers, one receiving, one replenishment, one manager")
    if len(set(a["badge"] for a in actors)) != 7:
        ap.error("Require seven distinct test badges")
    if cfg.get("isolated_database_confirmed") is not True or cfg.get("external_integrations_disabled") is not True:
        ap.error("Confirm separate test DB and disabled external writes in config")
    if not args.execute:
        print("DRY RUN ONLY. Target:", root, "Users: 7. No HTTP requests sent.")
        print("To execute: verify isolated test deployment/DB/integrations and add --execute")
        return
    barrier = threading.Barrier(7)
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(run_actor, root, actor, args.operations, barrier, args.timeout) for actor in actors]
        results = [future.result(timeout=max(60, args.operations*args.timeout+40)) for future in futures]
    latencies = sorted(x for r in results for x in r["latency_ms"])
    p95 = latencies[min(len(latencies)-1, max(0, (95*len(latencies)+99)//100-1))] if latencies else None
    summary = {
        "users": len(results), "all_success": all(r["ok"] for r in results),
        "requests": len(latencies), "median_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(p95, 1) if p95 is not None else None,
        "actors": [{k:v for k,v in r.items() if k!="latency_ms"} for r in results]
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if not summary["all_success"]:
        raise SystemExit(1)

if __name__ == "__main__":
    main()
