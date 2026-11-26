/* GRIT WMS: print console UX, isolated synthetic-only manager demo.
   Never emit USB commands, infer physical PRINTED, or accept raw HTML. */
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const STATUS = Object.freeze({
    QUEUED: ["В очереди", "neutral"],
    SENDING: ["Передаётся", "warn"],
    SUBMITTED: ["Передано виртуальному агенту", "good"],
    PRINTED: ["Отметка печати (тест)", "warn"],
    FAILED: ["Ошибка передачи", "error"],
    UNKNOWN: ["Требует проверки", "error"],
    CANCELLED: ["Отменено", "neutral"],
    EMPTY: ["Нет ожидающих заданий", "neutral"]
  });
  const EVENT = Object.freeze({
    TRANSITION: "Изменение статуса",
    ENQUEUE: "Добавлено в очередь",
    CLAIM: "Задание получено",
    RELEASE: "Задание освобождено",
    VIRTUAL_AGENT: "Виртуальный агент",
    USB_AGENT: "Windows-агент (тест)"
  });
  let csrf = "", busy = false, snapshot = null;
  let usbEnabled = false, usbActive = false;

  function node(tag, className, content) {
    const n = document.createElement(tag);
    if (className) n.className = className;
    if (content !== undefined && content !== null) n.textContent = String(content);
    return n;
  }
  function stateLabel(value) { return (STATUS[value] || [String(value || "—"), "neutral"])[0]; }
  function stateChip(value) {
    const style = (STATUS[value] || STATUS.EMPTY)[1];
    return node("span", "state state-" + style, stateLabel(value));
  }
  function printerName(id) {
    const p = snapshot && snapshot.printers.find(x => x.id === id);
    return p ? p.name : "Неизвестный принтер";
  }
  function printerLines(id) {
    const p = snapshot && snapshot.printers.find(x => x.id === id);
    return p ? p.lines.join(", ") : "—";
  }
  function setStatus(message, error) {
    $("status").textContent = message;
    $("status").dataset.state = error ? "error" : "ok";
  }
  function renderEmpty(element, columns, message) {
    const row = node("tr");
    const cell = node("td", "", message);
    cell.colSpan = columns;
    row.appendChild(cell);
    element.replaceChildren(row);
  }
  function number(value) {
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : 0;
  }
  function renderTotals() {
    const totals = snapshot.summary || {};
    $("metricQueued").textContent = number(totals.QUEUED);
    $("metricSubmitted").textContent = number(totals.SUBMITTED);
    $("metricAttention").textContent = number(totals.attention);
    $("metricTotal").textContent = number(totals.total);
    $("lastRefresh").textContent = "Обновлено: " + new Date().toLocaleTimeString("ru-RU",
      { hour: "2-digit", minute: "2-digit", second: "2-digit" }) +
      " · данные по всем заданиям, таблицы показывают последние 100";
  }
  function printerFact(value, label) {
    const wrap = node("div", "printer-fact");
    wrap.append(node("strong", "", number(value)), node("span", "", label));
    return wrap;
  }
  function renderPrinterCards() {
    const region = $("printerCards");
    region.replaceChildren();
    if (!snapshot.printers.length) {
      region.appendChild(node("div", "empty", "Принтеры пока не добавлены."));
      return;
    }
    snapshot.printers.forEach(printer => {
      const card = node("article", "printer-card");
      const top = node("div", "printer-card-top");
      const info = node("div");
      info.append(node("div", "printer-name", printer.name),
                  node("div", "printer-lines", "Линии: " + printer.lines.join(", ")));
      top.append(info,node("span",printer.enabled ?
        "state state-good" : "state state-error",
        printer.enabled ? "Активен" : "Отключён"));
      const facts = node("div", "printer-facts");
      facts.append(printerFact(printer.queued, "В очереди"),
                   printerFact(printer.attention, "Проверить"),
                   printerFact(printer.total, "Всего"));
      const recent = node("div", "printer-last");
      recent.append(node("span", "", "Последнее: "));
      recent.append(printer.last_state ? stateChip(printer.last_state) :
                    node("span", "", "нет операций"));
      if (printer.last_order) recent.append(node("span", "", " · " + printer.last_order));
      card.append(top,facts,recent);
      region.appendChild(card);
    });
  }
  function replaceOptions(select, options) {
    const prev = select.value;
    select.replaceChildren(...options);
    if ([...select.options].some(option => option.value === prev)) select.value = prev;
  }
  function populatePrinters() {
    const work = snapshot.printers.map(printer => {
      const option = node("option");
      option.value = printer.id;
      option.textContent = printer.name + " · " + printer.lines.join(", ") +
        (printer.enabled ? "" : " (отключён)");
      return option;
    });
    replaceOptions($("printer"),work);
    const filterOptions = [node("option", "", "Все принтеры")];
    filterOptions[0].value = "";
    snapshot.printers.forEach(printer => {
      const option = node("option", "", printer.name + " · " + printer.lines.join(", "));
      option.value = printer.id;
      filterOptions.push(option);
    });
    replaceOptions($("filterPrinter"),filterOptions);
    updateAgent();
  }
  function updateAgent() {
    const printer = snapshot && snapshot.printers.find(p => p.id === $("printer").value);
    const ready = !!printer && printer.enabled && number(printer.queued) > 0;
    $("dispatch").disabled = busy || !ready || usbActive;
    $("usbApprove").disabled = busy || !ready || !usbEnabled || usbActive;
    $("agentHint").textContent = !printer ? "Нет тестовых принтеров." :
      !printer.enabled ? "Принтер отключён." :
      !ready ? "На выбранном принтере нет заданий в очереди." :
      "В очереди на этом принтере: " + number(printer.queued) +
      ". Одна операция передаст ровно одно задание виртуальному агенту.";
  }
  function jobMatches(job) {
    const filter = $("filterPrinter").value;
    const state = $("filterState").value;
    const search = $("searchOrder").value.trim().toLowerCase();
    return (!filter || job.printer_id === filter) &&
      (!state || job.state === state) &&
      (!search || String(job.order_id).toLowerCase().includes(search));
  }
  function renderJobs() {
    const rows = $("jobs");
    rows.replaceChildren();
    const visible = snapshot.jobs.filter(jobMatches);
    if (!visible.length) renderEmpty(rows, 4, "По выбранным фильтрам заданий нет.");
    visible.forEach(job => {
      const tr = node("tr");
      tr.appendChild(node("td", "", job.order_id));
      const printer = node("td");
      printer.append(node("div", "", printerName(job.printer_id)),
                     node("div", "cell-secondary", "Линии: " + printerLines(job.printer_id)));
      tr.append(printer,node("td"));
      tr.lastChild.append(stateChip(job.state));
      tr.appendChild(node("td", "", job.version));
      rows.appendChild(tr);
    });
    $("filteredCount").textContent = "Показано " + visible.length +
      " из " + snapshot.jobs.length + " последних заданий";
  }
  function renderEvents() {
    const rows = $("events");
    rows.replaceChildren();
    if (!snapshot.events.length) renderEmpty(rows, 4, "Событий пока нет.");
    snapshot.events.forEach(event => {
      const tr = node("tr");
      tr.appendChild(node("td", "", event.order_id));
      tr.appendChild(node("td", "", EVENT[event.event] || event.event || "Событие"));
      const path = node("td");
      path.append(node("span", "", event.from ? stateLabel(event.from) + " → " : "Начало → "));
      path.appendChild(stateChip(event.to));
      tr.appendChild(path);
      tr.appendChild(node("td", "", event.reason === "VIRTUAL_SINK_RESULT" ?
        "Подтверждение виртуального агента" :
        event.reason === "SYNTHETIC_QR_SCAN" ? "По QR принтера" :
        event.reason === "VIRTUAL_CLAIM" ? "Задание взято агентом" :
        event.reason || "—"));
      rows.appendChild(tr);
    });
    // Diagnostic codes are text only, with no original stickers or credentials.
    $("technicalEvents").textContent = snapshot.events.map(event =>
      [event.order_id,event.event,event.from || "START",event.to,event.reason].join(" | ")
    ).join("\n");
  }
  function verifySynthetic(data) {
    if (!data || data.synthetic !== true || data.physical_printing !== false ||
        !data.summary || !Array.isArray(data.printers) ||
        !Array.isArray(data.jobs) || !Array.isArray(data.events)) {
      throw new Error("Отказ: данные не из изолированного тестового стенда.");
    }
  }
  async function api(path, method, data) {
    const res = await fetch(path,{
      method: method || "GET", credentials: "same-origin", cache: "no-store",
      headers: data ? {"Content-Type":"application/json"} : {},
      body: data ? JSON.stringify(data) : undefined
    });
    const payload = await res.json();
    if (!res.ok) throw new Error(payload.error || "Сервер GRIT недоступен.");
    return payload;
  }
  async function refresh() {
    const data = await api("/demo/printing/state");
    verifySynthetic(data);
    snapshot = data;
    csrf = data.manager_csrf;
    renderTotals();
    renderPrinterCards();
    populatePrinters();
    renderJobs();
    renderEvents();
    await refreshUsb();
  }
  async function refreshUsb() {
    try {
      const result = await api("/demo/printing/usb/state");
      if (result.synthetic !== true || result.physical_printing !== false ||
          result.enabled !== true || !Array.isArray(result.approvals)) {
        throw new Error("Неверный ответ USB-стенда");
      }
      usbEnabled = true;
      usbActive = result.approvals.some(a => a.phase === "APPROVED" || a.phase === "CLAIMED");
      const latest = result.approvals[0];
      $("usbState").textContent = !latest
        ? "USB-стенд готов. Разрешённых заданий пока нет."
        : "Последнее: " + latest.order_id + " · " + latest.phase +
          (latest.result ? " · " + latest.result : "") +
          ". Повторная печать после UNKNOWN запрещена.";
      $("usbState").dataset.state = "ok";
    } catch (error) {
      usbEnabled = false;
      usbActive = false;
      $("usbState").textContent = "USB-стенд недоступен: " + error.message;
      $("usbState").dataset.state = "error";
    }
    updateAgent();
  }
  async function approveUsb() {
    if (busy || !usbEnabled || usbActive || !snapshot) return;
    busy = true;
    updateAgent();
    try {
      const printer_id = $("printer").value;
      if (!printer_id || !csrf) throw new Error("Нет принтера или CSRF");
      const result = await api("/demo/printing/usb/approve","POST",{printer_id,csrf});
      if (result.synthetic !== true || result.physical_printing !== false) {
        throw new Error("Отказ: не тестовая операция");
      }
      await refresh();
      setStatus(result.state === "APPROVED" ?
        "Разрешено одно синтетическое задание для Windows-агента. Бумажной печати пока НЕТ." :
        "USB: " + (result.reason || result.state) + ". Физическая печать не запускалась.",false);
    } catch (error) {
      setStatus(error.message || "Ошибка разрешения USB",true);
      await refreshUsb();
    } finally {
      busy = false;
      updateAgent();
    }
  }
  async function dispatch() {
    if (busy || !snapshot) return;
    busy = true;
    updateAgent();
    try {
      const printer_id = $("printer").value;
      const found = snapshot.printers.find(p => p.id === printer_id);
      if (!found || !found.enabled || !number(found.queued) || !csrf) {
        throw new Error("На принтере нет доступного тестового задания.");
      }
      const result = await api("/demo/printing/virtual-deliver","POST",{printer_id,csrf});
      if (result.synthetic !== true || result.physical_printing !== false) {
        throw new Error("Отказ: неожиданное подтверждение печати.");
      }
      await refresh();
      const message = result.state === "SUBMITTED"
        ? "Задание принято виртуальным агентом. Физической печати не было."
        : result.state === "EMPTY"
          ? "Очередь уже пуста. Повторная передача не выполнялась."
          : "Результат: " + stateLabel(result.state) +
            ". Физическая печать не подтверждена.";
      setStatus(message,false);
    } catch (error) {
      setStatus(error.message || "Ошибка связи с тестовым сервером.",true);
    } finally {
      busy = false;
      updateAgent();
    }
  }
  document.addEventListener("DOMContentLoaded",() => {
    $("refresh").addEventListener("click", async () => {
      try {
        await refresh();
        setStatus("Данные очереди обновлены.",false);
      } catch (error) {
        setStatus(error.message,true);
      }
    });
    $("dispatch").addEventListener("click",dispatch);
    $("usbApprove").addEventListener("click",approveUsb);
    $("usbRefresh").addEventListener("click",refreshUsb);
    $("printer").addEventListener("change",updateAgent);
    ["searchOrder","filterPrinter","filterState"].forEach(id => {
      $(id).addEventListener(id === "searchOrder" ? "input" : "change",() => {
        if (snapshot) renderJobs();
      });
    });
    refresh().then(()=>setStatus("Панель готова. Только синтетические операции.",false))
      .catch(error=>setStatus(error.message,true));
  });
})();
