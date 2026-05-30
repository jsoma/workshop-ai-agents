// ==UserScript==
// @name         North Dakota HazConnect Incident Exporter
// @namespace    https://northdakota.hazconnect.com/
// @version      0.1.0
// @description  Export HazConnect incident table pages and download accessible public incident PDFs.
// @match        https://northdakota.hazconnect.com/ListIncidentPublic.aspx*
// @match        https://northdakota.hazconnect.com/ViewPdfData.aspx*
// @grant        GM_registerMenuCommand
// @run-at       document-idle
// ==/UserScript==

(function () {
  "use strict";

  const state = {
    rows: [],
    seen: new Set(),
    running: false,
    stop: false,
    delayMs: 900,
    pdfDelayMs: 1200,
    maxPages: 500,
    downloadHtmlInsteadOfPdf: false,
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function cleanText(value) {
    return String(value || "")
      .replace(/\u00a0/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  function absoluteUrl(value) {
    if (!value) return "";
    try {
      return new URL(value, window.location.href).toString();
    } catch {
      return "";
    }
  }

  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }

  function findIncidentTable() {
    const tables = Array.from(document.querySelectorAll("table"));
    return (
      tables.find((table) => /Incident ID/i.test(table.innerText) && /Incident Type/i.test(table.innerText)) ||
      tables.find((table) => table.querySelector('a[href*="ViewPdfData.aspx"]')) ||
      null
    );
  }

  function getHeaders(table) {
    const headerRows = Array.from(table.querySelectorAll("thead tr, tr")).slice(0, 6);
    for (const row of headerRows) {
      const cells = Array.from(row.querySelectorAll("th,td")).map((cell) => cleanText(cell.innerText));
      if (cells.includes("Incident ID") && cells.includes("Incident Type")) return cells;
    }
    return [
      "Incident ID",
      "Incident Type",
      "Incident Date",
      "County/Tribe",
      "Contained",
      "Date Reported",
      "Chemicals",
      "Section",
      "Township",
      "Range",
    ];
  }

  function normalizeRow(headers, row) {
    const cells = Array.from(row.querySelectorAll("td"));
    const reportAnchor = row.querySelector('a[href*="ViewPdfData.aspx"]');
    if (!reportAnchor || cells.length < 4) return null;

    const values = cells.map((cell) => cleanText(cell.innerText));
    const byHeader = {};
    for (let i = 0; i < Math.min(headers.length, values.length); i += 1) {
      if (headers[i]) byHeader[headers[i]] = values[i];
    }

    const reportUrl = absoluteUrl(reportAnchor.getAttribute("href"));
    const reportIdMatch = reportUrl.match(/[?&]ReportID=(\d+)/i);
    const incidentId = cleanText(reportAnchor.innerText) || byHeader["Incident ID"] || values[0] || "";

    return {
      incident_id: incidentId,
      incident_type: byHeader["Incident Type"] || values[1] || "",
      incident_date: byHeader["Incident Date"] || values[2] || "",
      county_tribe: byHeader["County/Tribe"] || values[3] || "",
      contained: byHeader.Contained || values[4] || "",
      date_reported: byHeader["Date Reported"] || values[5] || "",
      chemicals: byHeader.Chemicals || values[6] || "",
      section: byHeader.Section || values[7] || "",
      township: byHeader.Township || values[8] || "",
      range: byHeader.Range || values[9] || "",
      report_id: reportIdMatch ? reportIdMatch[1] : "",
      report_url: reportUrl,
      source_url: window.location.href,
      captured_at: new Date().toISOString(),
    };
  }

  function extractCurrentPageRows() {
    const table = findIncidentTable();
    if (!table) throw new Error("Could not find the HazConnect incident table.");
    const headers = getHeaders(table);
    return Array.from(table.querySelectorAll("tr"))
      .map((row) => normalizeRow(headers, row))
      .filter(Boolean);
  }

  function addRows(rows) {
    let added = 0;
    for (const row of rows) {
      const key = row.report_id || row.report_url || `${row.incident_id}:${row.incident_date}`;
      if (state.seen.has(key)) continue;
      state.seen.add(key);
      state.rows.push(row);
      added += 1;
    }
    return added;
  }

  function tableSignature() {
    const rows = extractCurrentPageRows();
    return rows.map((row) => row.report_id || row.incident_id).slice(0, 20).join("|");
  }

  function findNextPageControl() {
    const controls = Array.from(document.querySelectorAll("a,button,input[type='button'],input[type='submit']"));
    const candidates = controls.filter((el) => {
      if (!isVisible(el)) return false;
      if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
      if (/\bdisabled\b/i.test(el.className || "") || /\bdisabled\b/i.test(el.parentElement?.className || "")) return false;
      const text = cleanText(el.innerText || el.value || el.title || el.getAttribute("aria-label") || "");
      const href = el.getAttribute("href") || "";
      const onclick = el.getAttribute("onclick") || "";
      const haystack = `${text} ${href} ${onclick}`;
      return /(^|[^a-z])(next|>)([^a-z]|$)/i.test(haystack) && !/\blast\b/i.test(haystack);
    });
    return candidates[0] || null;
  }

  async function clickNextPage() {
    const control = findNextPageControl();
    if (!control) return false;
    const before = tableSignature();
    control.click();
    const started = Date.now();
    while (Date.now() - started < 15000) {
      await sleep(250);
      try {
        if (tableSignature() !== before) return true;
      } catch {
        // Table may be briefly absent during an ASP.NET partial refresh.
      }
    }
    return tableSignature() !== before;
  }

  function toCsv(rows) {
    const columns = [
      "incident_id",
      "incident_type",
      "incident_date",
      "county_tribe",
      "contained",
      "date_reported",
      "chemicals",
      "section",
      "township",
      "range",
      "report_id",
      "report_url",
      "source_url",
      "captured_at",
    ];
    const quote = (value) => `"${String(value ?? "").replace(/"/g, '""')}"`;
    return [columns.join(","), ...rows.map((row) => columns.map((col) => quote(row[col])).join(","))].join("\n");
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  function downloadText(text, filename, type) {
    downloadBlob(new Blob([text], { type }), filename);
  }

  function safeFilename(value) {
    return String(value || "hazconnect")
      .replace(/[^a-z0-9._-]+/gi, "_")
      .replace(/^_+|_+$/g, "")
      .slice(0, 160);
  }

  function hasChallengeText(text) {
    return /ValidateCaptcha|Please enter the characters|captcha|Incapsula|_Incapsula_Resource/i.test(text);
  }

  async function downloadReport(row) {
    const response = await fetch(row.report_url, {
      credentials: "include",
      headers: { Accept: "application/pdf,text/html,*/*" },
    });
    const contentType = response.headers.get("content-type") || "";
    const blob = await response.blob();
    const prefix = safeFilename(`${row.incident_id || row.report_id}_${row.report_id || "report"}`);

    if (/pdf/i.test(contentType)) {
      downloadBlob(blob, `${prefix}.pdf`);
      return { ok: true, type: "pdf", status: response.status };
    }

    const sample = await blob.slice(0, 4096).text();
    if (sample.startsWith("%PDF")) {
      downloadBlob(blob, `${prefix}.pdf`);
      return { ok: true, type: "pdf", status: response.status };
    }
    if (hasChallengeText(sample)) {
      return { ok: false, type: "challenge", status: response.status };
    }
    if (state.downloadHtmlInsteadOfPdf) {
      downloadBlob(blob, `${prefix}.html`);
      return { ok: true, type: "html", status: response.status };
    }
    return { ok: false, type: contentType || "unknown", status: response.status };
  }

  function ensurePanel() {
    let panel = document.getElementById("hazconnect-exporter-panel");
    if (panel) return panel;

    panel = document.createElement("div");
    panel.id = "hazconnect-exporter-panel";
    panel.innerHTML = `
      <div class="haz-title">HazConnect Exporter</div>
      <div class="haz-status" id="hazconnect-exporter-status">Ready.</div>
      <div class="haz-buttons">
        <button type="button" data-action="current">Collect current page</button>
        <button type="button" data-action="all">Collect all pages</button>
        <button type="button" data-action="csv">Export CSV</button>
        <button type="button" data-action="json">Export JSON</button>
        <button type="button" data-action="pdfs">Download PDFs</button>
        <button type="button" data-action="stop">Stop</button>
      </div>
    `;
    const style = document.createElement("style");
    style.textContent = `
      #hazconnect-exporter-panel {
        position: fixed;
        right: 16px;
        bottom: 16px;
        z-index: 2147483647;
        width: 310px;
        padding: 12px;
        background: #111827;
        color: #f9fafb;
        border: 1px solid #374151;
        border-radius: 8px;
        box-shadow: 0 16px 40px rgba(0,0,0,.35);
        font: 13px/1.4 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      #hazconnect-exporter-panel .haz-title { font-weight: 700; margin-bottom: 6px; }
      #hazconnect-exporter-panel .haz-status { min-height: 38px; color: #d1d5db; margin-bottom: 8px; white-space: pre-wrap; }
      #hazconnect-exporter-panel .haz-buttons { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
      #hazconnect-exporter-panel button {
        border: 1px solid #4b5563;
        border-radius: 6px;
        background: #1f2937;
        color: #f9fafb;
        padding: 7px 8px;
        cursor: pointer;
      }
      #hazconnect-exporter-panel button:hover { background: #374151; }
    `;
    document.documentElement.appendChild(style);
    document.body.appendChild(panel);

    panel.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-action]");
      if (!button) return;
      const action = button.dataset.action;
      try {
        if (action === "current") await collectCurrentPage();
        if (action === "all") await collectAllPages();
        if (action === "csv") exportCsv();
        if (action === "json") exportJson();
        if (action === "pdfs") await downloadAllPdfs();
        if (action === "stop") {
          state.stop = true;
          setStatus("Stopping after the current request finishes.");
        }
      } catch (error) {
        setStatus(`Error: ${error.message || error}`);
      }
    });

    return panel;
  }

  function setStatus(message) {
    ensurePanel().querySelector("#hazconnect-exporter-status").textContent = message;
  }

  async function collectCurrentPage() {
    const rows = extractCurrentPageRows();
    const added = addRows(rows);
    setStatus(`Collected ${rows.length} rows from current page.\nTotal unique rows: ${state.rows.length}. Added: ${added}.`);
  }

  async function collectAllPages() {
    if (state.running) return;
    state.running = true;
    state.stop = false;
    try {
      let pageCount = 0;
      while (!state.stop && pageCount < state.maxPages) {
        pageCount += 1;
        const rows = extractCurrentPageRows();
        const added = addRows(rows);
        setStatus(`Page ${pageCount}: ${rows.length} rows, ${added} new.\nTotal unique rows: ${state.rows.length}.`);
        await sleep(state.delayMs);
        const moved = await clickNextPage();
        if (!moved) break;
        await sleep(state.delayMs);
      }
      setStatus(`Done collecting pages.\nPages visited: ${pageCount}. Unique rows: ${state.rows.length}.`);
    } finally {
      state.running = false;
    }
  }

  function exportCsv() {
    const rows = state.rows.length ? state.rows : extractCurrentPageRows();
    downloadText(toCsv(rows), `hazconnect_incidents_${new Date().toISOString().slice(0, 10)}.csv`, "text/csv;charset=utf-8");
    setStatus(`CSV export queued for ${rows.length} rows.`);
  }

  function exportJson() {
    const rows = state.rows.length ? state.rows : extractCurrentPageRows();
    downloadText(
      JSON.stringify(rows, null, 2),
      `hazconnect_incidents_${new Date().toISOString().slice(0, 10)}.json`,
      "application/json;charset=utf-8",
    );
    setStatus(`JSON export queued for ${rows.length} rows.`);
  }

  async function downloadAllPdfs() {
    if (state.running) return;
    state.running = true;
    state.stop = false;
    const rows = state.rows.length ? state.rows : extractCurrentPageRows();
    const reportRows = rows.filter((row) => row.report_url);
    let ok = 0;
    let failed = 0;
    const failures = [];
    try {
      for (let i = 0; i < reportRows.length; i += 1) {
        if (state.stop) break;
        const row = reportRows[i];
        setStatus(`Downloading ${i + 1}/${reportRows.length}: incident ${row.incident_id}`);
        try {
          const result = await downloadReport(row);
          if (result.ok) ok += 1;
          else {
            failed += 1;
            failures.push({ incident_id: row.incident_id, report_id: row.report_id, report_url: row.report_url, reason: result.type });
          }
        } catch (error) {
          failed += 1;
          failures.push({ incident_id: row.incident_id, report_id: row.report_id, report_url: row.report_url, reason: String(error.message || error) });
        }
        await sleep(state.pdfDelayMs);
      }
      if (failures.length) {
        downloadText(
          JSON.stringify(failures, null, 2),
          `hazconnect_pdf_failures_${new Date().toISOString().slice(0, 10)}.json`,
          "application/json;charset=utf-8",
        );
      }
      setStatus(`PDF download pass complete.\nSaved: ${ok}. Failed/skipped: ${failed}.`);
    } finally {
      state.running = false;
    }
  }

  window.hazconnectExporter = {
    state,
    collectCurrentPage,
    collectAllPages,
    exportCsv,
    exportJson,
    downloadAllPdfs,
    extractCurrentPageRows,
  };

  ensurePanel();
  GM_registerMenuCommand?.("HazConnect: collect current page", collectCurrentPage);
  GM_registerMenuCommand?.("HazConnect: collect all pages", collectAllPages);
  GM_registerMenuCommand?.("HazConnect: download PDFs", downloadAllPdfs);
})();
