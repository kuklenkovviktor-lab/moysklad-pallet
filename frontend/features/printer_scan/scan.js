/* GRIT WMS FSD feature: printer QR -> automatic queue, SYNTHETIC ONLY.
 * This script is used only by the isolated /demo/worker-qr page.
 * The real /fbs handler in wms.py is not modified by this PR.
 * A PRINTER QR is NOT an order barcode; the client never chooses order IDs.
 */
(function (scope) {
  "use strict";
  const PREFIX = "GRIT-PRINTER:";
  const SESSION_URL = "/synthetic/pr192/print-queue/session";
  const SCAN_URL = "/synthetic/pr192/print-queue/scan";
  let busy = false;

  function matches(code) {
    return typeof code === "string" && code.trim().startsWith(PREFIX);
  }

  async function submit(code, report) {
    if (!matches(code)) return false;
    if (busy) return true;
    busy = true;
    const notify = typeof report === "function" ? report : function () {};
    try {
      const session = await fetch(SESSION_URL, {
        credentials: "same-origin", cache: "no-store",
        headers: { "Accept": "application/json" }
      });
      const identity = await session.json();
      if (!session.ok || !identity.csrf) throw new Error(identity.error || "Сессия сборщика не подтверждена");
      const response = await fetch(SCAN_URL, {
        method: "POST", credentials: "same-origin", cache: "no-store",
        headers: { "Content-Type": "application/json", "Accept": "application/json" },
        body: JSON.stringify({ qr: code.trim(), csrf: identity.csrf })
      });
      const result = await response.json();
      if (!response.ok || !result.ok || !result.synthetic) {
        throw new Error(result.error || "QR принтера отклонён");
      }
      notify({
        ok: true,
        text: "В очередь: " + result.queued_new + " новых; "
              + result.already_queued + " уже в очереди; "
              + result.requires_review + " на проверку. Печать ещё не подтверждена.",
        data: result
      });
    } catch (error) {
      notify({ ok: false, text: error && error.message ? error.message : "Нет ответа сервера" });
    } finally {
      busy = false;
    }
    return true;
  }

  function attach(input, status) {
    if (!input || !status) throw new Error("Missing scanner elements");
    function report(message) {
      status.textContent = message.text;
      status.dataset.state = message.ok ? "ok" : "error";
    }
    function scan(value) {
      if (matches(value)) return submit(value, report);
      report({ ok: false, text: "Это синтетическое демо: требуется QR принтера GRIT. Товарные ШК здесь не обрабатываются." });
      return Promise.resolve(false);
    }
    // HID-ring and TSD scanning: Enter-terminated scan into the focused input.
    input.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      event.preventDefault();
      const value = input.value;
      input.value = "";
      scan(value);
      input.focus();
    });
    let buffer = "", lastAt = 0, resetTimer = null;
    // Keep working when an HID scanner focuses outside the text input.
    document.addEventListener("keydown", function (event) {
      if (document.hidden || event.target === input || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.target && (event.target.isContentEditable ||
          ["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName))) return;
      if (event.key === "Enter" || event.key === "Tab") {
        if (buffer.length >= 4) {
          event.preventDefault();
          const value = buffer; buffer = "";
          clearTimeout(resetTimer); scan(value);
        } else buffer = "";
        return;
      }
      if (event.key.length !== 1) return;
      const now = Date.now();
      if (now - lastAt > 350) buffer = "";
      lastAt = now;
      if (buffer.length < 256) buffer += event.key;
      clearTimeout(resetTimer);
      resetTimer = setTimeout(function () { buffer = ""; }, 900);
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) input.focus();
    });
    setTimeout(function () { input.focus(); }, 50);
    return { scan: scan };
  }

  scope.GRITPrinterScan = Object.freeze({ matches: matches, submit: submit, attach: attach });
})(window);
