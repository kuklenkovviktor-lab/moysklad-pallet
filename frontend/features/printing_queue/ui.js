/* GRIT FSD printing_queue feature: synthetic-only manager dashboard.
   No physical printing. No browser-supplied job IDs or delivery modes. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  let csrf = "", busy = false;
  const STATES = ["QUEUED","SENDING","SUBMITTED","PRINTED","FAILED","UNKNOWN","CANCELLED"];

  function td(value) {
    const cell = document.createElement("td");
    cell.textContent = value == null ? "—" : String(value);
    return cell;
  }
  function setStatus(message, isError) {
    const el = $("status");
    el.textContent = message;
    el.dataset.state = isError ? "error" : "ok";
  }
  function renderRows(element, rows, fields) {
    element.replaceChildren();
    rows.forEach(item => {
      const tr = document.createElement("tr");
      fields.forEach(field => tr.appendChild(td(item[field])));
      element.appendChild(tr);
    });
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = td("Нет записей");
      cell.colSpan = fields.length;
      row.appendChild(cell);
      element.appendChild(row);
    }
  }
  async function api(path, method, data) {
    const res = await fetch(path, {
      method: method || "GET", credentials: "same-origin",
      cache: "no-store",
      headers: data ? { "Content-Type": "application/json" } : {},
      body: data ? JSON.stringify(data) : undefined
    });
    const json = await res.json();
    if (!res.ok) throw new Error(json.error || "Нет ответа от GRIT");
    return json;
  }
  async function refresh() {
    const data = await api("/demo/printing/state");
    if (!data.synthetic || data.physical_printing !== false) {
      throw new Error("REFUSING_NON_SYNTHETIC_DATA");
    }
    csrf = data.manager_csrf;
    const select = $("printer");
    const old = select.value;
    select.replaceChildren();
    data.printers.forEach(printer => {
      const option = document.createElement("option");
      option.value = printer.id;
      option.textContent = printer.name + " · " +
        printer.lines.join(", ") + (printer.enabled ? "" : " · ОТКЛ.");
      select.appendChild(option);
    });
    if (data.printers.some(item => item.id === old)) select.value = old;
    $("dispatch").disabled = !select.value;
    const totals = {};
    STATES.forEach(s => { totals[s] = 0; });
    data.jobs.forEach(job => { if (STATES.includes(job.state)) totals[job.state]++; });
    $("counts").textContent = STATES.map(state => state + ": " + totals[state]).join("  ·  ");
    renderRows($("jobs"), data.jobs, ["order_id","printer_id","state","version"]);
    renderRows($("events"), data.events, ["order_id","event","from","to","reason"]);
    $("lastRefresh").textContent = "Показано заданий: " + data.jobs.length +
      " · событий: " + data.events.length + " (последние 100)";
  }
  async function dispatch() {
    if (busy) return;
    busy = true;
    $("dispatch").disabled = true;
    try {
      const printer_id = $("printer").value;
      if (!printer_id || !csrf) throw new Error("Выбери принтер и обнови данные");
      const result = await api("/demo/printing/virtual-deliver", "POST",
                               {printer_id, csrf});
      if (!result.synthetic || result.physical_printing !== false) {
        throw new Error("Получен неожиданный ответ сервера");
      }
      setStatus("Виртуальный агент: " + result.state +
                " · " + result.reason + ". Физической печати нет.",false);
      await refresh();
    } catch (error) {
      setStatus(error.message || "Не удалось выполнить запрос",true);
    } finally {
      busy = false;
      $("dispatch").disabled = !$("printer").value;
    }
  }
  document.addEventListener("DOMContentLoaded",() => {
    $("refresh").addEventListener("click",() => {
      refresh().then(()=>setStatus("Данные очереди обновлены.",false))
               .catch(error=>setStatus(error.message,true));
    });
    $("dispatch").addEventListener("click",dispatch);
    refresh().then(()=>setStatus("Данные очереди загружены. Только синтетика.",false))
             .catch(error=>setStatus(error.message,true));
  });
})();
