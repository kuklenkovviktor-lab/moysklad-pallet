// Node-only synthetic regression for the ACTUAL existing /fbs scanner handler.
// No network, database, USB, production orders or employee page.
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const src = fs.readFileSync(path.join(__dirname, "../../wms.py"), "utf8");
const from = src.indexOf("// Single existing /fbs scanner handles printer QR;");
const to = src.indexOf("// Android HID scanners", from);
assert(from > 0 && to > from, "existing worker scanner integration must be present");
const code = src.slice(from, to);
assert(code.includes("if(code.startsWith('GRIT-PRINTER:'))await acceptPrinterQr(code);"));
assert(code.includes("else if(pendingProduct)await acceptWb(code);"));
assert(code.includes("else await acceptProduct(code);"));

async function trial({ pending = null, authStatus = 200, scanStatus = 200, scanPayload = null, input = "GRIT-PRINTER:1:demo-printer:synthetic-test-token" } = {}) {
  const requests = [], beeps = [], errors = [], accepted = [];
  const element = { textContent: "", style: {}, value: "", focus() {} };
  const sandbox = {
    pendingProduct: pending,
    shiftId: "synthetic-shift",
    busy: false,
    document: { getElementById() { return element; } },
    setTimeout(callback) { callback(); },
    beep(value) { beeps.push(value); },
    showTsdError(text) { errors.push(text); },
    queuedFbsScan() { return null; },
    acceptProduct(code) { accepted.push(["product", code]); },
    acceptWb(code) { accepted.push(["wb", code]); },
    acceptSkipQr(code) { accepted.push(["skip", code]); },
    async refresh() { throw new Error("must not refresh real picking"); },
    async retryFbsOutbox() { throw new Error("must not use FBS outbox"); },
    fetch: async (url, opts) => {
      requests.push({ url, opts });
      if (url.endsWith("/session")) return {
        ok: authStatus === 200,
        async json() { return { synthetic: true, csrf: "synthetic-csrf" }; }
      };
      if (url.endsWith("/scan")) return {
        ok: scanStatus === 200,
        async json() {
          return scanPayload || {
            ok: true, synthetic: true, physical_printing: false,
            queued_new: 1, already_queued: 0
          };
        }
      };
      throw new Error("unapproved endpoint: " + url);
    }
  };
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  await sandbox.handleScan(input);
  return { requests, beeps, errors, accepted, element, sandbox };
}

(async () => {
  const good = await trial();
  assert.equal(good.requests.length, 2);
  assert.equal(good.requests[0].url, "/synthetic/pr192/print-queue/session");
  assert.equal(good.requests[1].url, "/synthetic/pr192/print-queue/scan");
  const body = JSON.parse(good.requests[1].opts.body);
  assert.deepEqual(Object.keys(body).sort(), ["csrf","qr"]);
  assert.equal(body.qr.startsWith("GRIT-PRINTER:"), true);
  assert.equal(good.accepted.length, 0);
  assert.equal(good.beeps.length, 1);
  assert.match(good.element.textContent, /в очереди: 1 новых/);

  const pending = await trial({pending: {barcode: "SYNTH-SKU-B"}});
  assert.equal(pending.requests.length, 0, "printer QR must not interrupt pending product/WB pair");
  assert.equal(pending.accepted.length, 0);
  assert.match(pending.errors[0], /Сначала заверши/);

  const denied = await trial({authStatus: 401});
  assert.equal(denied.requests.length, 1);
  assert.equal(denied.accepted.length, 0);
  assert.equal(denied.beeps.length, 0);

  const revoked = await trial({scanStatus: 403, scanPayload: {error: "QR_REVOKED_OR_PRINTER_DISABLED"}});
  assert.equal(revoked.requests.length, 2);
  assert.equal(revoked.accepted.length, 0);
  assert.equal(revoked.beeps.length, 0);

  const invalid = await trial({scanPayload: {
    ok: true, synthetic: true, physical_printing: true, queued_new: 1
  }});
  assert.equal(invalid.accepted.length, 0);
  assert.equal(invalid.beeps.length, 0, "must not accept physical flag from QR endpoint");

  const regular = await trial({input: "2000000006390"});
  assert.equal(regular.requests.length, 0, "ordinary merchandise barcode must not call printer endpoint");
  assert.equal(regular.accepted.length, 1);
  assert.equal(regular.accepted[0][0], "product");

  const wb = await trial({pending: {barcode: "SYNTH-SKU-B"}, input: "SYNTH-WB-001"});
  assert.equal(wb.requests.length, 0);
  assert.equal(wb.accepted.length, 1);
  assert.equal(wb.accepted[0][0], "wb");

  console.log("PR209 EXISTING FBS SCANNER PRINTER QR SYNTHETIC PASS: 7 scenarios, NO USB/NETWORK");
})().catch(e => {console.error(e);process.exitCode = 1;});
