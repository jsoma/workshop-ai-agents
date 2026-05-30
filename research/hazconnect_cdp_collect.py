from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MATCH_URL = "northdakota.hazconnect.com/ListIncidentPublic.aspx"


COLLECT_ROWS_JS = r"""
() => {
  function cleanText(value) {
    return String(value || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
  }
  function absoluteUrl(value) {
    if (!value) return "";
    try { return new URL(value, window.location.href).toString(); } catch { return ""; }
  }
  function findIncidentTable() {
    const tables = Array.from(document.querySelectorAll("table"));
    return tables.find((table) => /Incident ID/i.test(table.innerText) && /Incident Type/i.test(table.innerText))
      || tables.find((table) => table.querySelector('a[href*="ViewPdfData.aspx"]'))
      || null;
  }
  function getHeaders(table) {
    const headerRows = Array.from(table.querySelectorAll("thead tr, tr")).slice(0, 6);
    for (const row of headerRows) {
      const cells = Array.from(row.querySelectorAll("th,td")).map((cell) => cleanText(cell.innerText));
      if (cells.includes("Incident ID") && cells.includes("Incident Type")) return cells;
    }
    return ["Incident ID", "Incident Type", "Incident Date", "County/Tribe", "Contained", "Date Reported", "Chemicals", "Section", "Township", "Range"];
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
  const table = findIncidentTable();
  if (!table) return { ok: false, error: "incident table not found", rows: [], signature: "" };
  const headers = getHeaders(table);
  const rows = Array.from(table.querySelectorAll("tr")).map((row) => normalizeRow(headers, row)).filter(Boolean);
  const signature = rows.map((row) => row.report_id || row.incident_id).slice(0, 20).join("|");
  return { ok: true, rows, signature };
}
"""


CLICK_NEXT_JS = r"""
() => {
  function cleanText(value) {
    return String(value || "").replace(/\u00a0/g, " ").replace(/\s+/g, " ").trim();
  }
  function isVisible(el) {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  const controls = Array.from(document.querySelectorAll("a,button,input[type='button'],input[type='submit']"));
  const next = controls.find((el) => {
    if (!isVisible(el)) return false;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return false;
    if (/\bdisabled\b/i.test(el.className || "") || /\bdisabled\b/i.test(el.parentElement?.className || "")) return false;
    const text = cleanText(el.innerText || el.value || el.title || el.getAttribute("aria-label") || "");
    const href = el.getAttribute("href") || "";
    const onclick = el.getAttribute("onclick") || "";
    const haystack = `${text} ${href} ${onclick}`;
    return /(^|[^a-z])(next|>)([^a-z]|$)/i.test(haystack) && !/\blast\b/i.test(haystack);
  });
  if (!next) return false;
  next.click();
  return true;
}
"""


@dataclass
class PdfResult:
    incident_id: str
    report_id: str
    report_url: str
    ok: bool
    status: int | None
    content_type: str
    output_path: str | None = None
    reason: str | None = None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("_")
    return cleaned[:160] or "hazconnect_report"


def dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique = []
    for row in rows:
        key = row.get("report_id") or row.get("report_url") or f"{row.get('incident_id')}:{row.get('incident_date')}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def write_rows(rows: list[dict[str, Any]], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "hazconnect_incidents.json"
    csv_path = output_dir / "hazconnect_incidents.csv"
    json_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    columns = [
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
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return json_path, csv_path


def find_hazconnect_page(browser: Any, url_match: str) -> Any:
    pages = [page for context in browser.contexts for page in context.pages]
    for page in pages:
        if url_match in page.url:
            return page
    open_pages = "\n".join(f"- {page.url}" for page in pages[:20])
    raise RuntimeError(
        f"No open Chrome tab matched {url_match!r}. Open the HazConnect list page in a Chrome instance "
        f"started with --remote-debugging-port, then retry.\nOpen CDP tabs:\n{open_pages}"
    )


def collect_pages(page: Any, max_pages: int, delay_ms: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    previous_signature = ""
    for page_number in range(1, max_pages + 1):
        result = page.evaluate(COLLECT_ROWS_JS)
        if not result.get("ok"):
            raise RuntimeError(result.get("error") or "Could not collect current page rows.")
        page_rows = result["rows"]
        rows.extend(page_rows)
        print(f"page {page_number}: {len(page_rows)} rows, {len(dedupe_rows(rows))} unique total")

        current_signature = result.get("signature") or ""
        clicked = page.evaluate(CLICK_NEXT_JS)
        if not clicked:
            break
        try:
            page.wait_for_function(
                """(previous) => {
                    const anchors = [...document.querySelectorAll('a[href*="ViewPdfData.aspx"]')];
                    const signature = anchors.slice(0, 20).map((a) => {
                      const m = a.href.match(/[?&]ReportID=(\d+)/i);
                      return m ? m[1] : a.textContent.trim();
                    }).join("|");
                    return signature && signature !== previous;
                }""",
                arg=current_signature or previous_signature,
                timeout=15000,
            )
        except Exception:
            print("warning: next-page click did not produce a detectable table change; stopping")
            break
        page.wait_for_timeout(delay_ms)
        previous_signature = current_signature
    return dedupe_rows(rows)


def content_has_challenge(body: bytes) -> bool:
    sample = body[:4096].decode("utf-8", errors="ignore")
    return bool(re.search(r"ValidateCaptcha|Please enter the characters|captcha|Incapsula|_Incapsula_Resource", sample, re.I))


def download_pdfs(page: Any, rows: list[dict[str, Any]], output_dir: Path, delay_ms: int) -> list[PdfResult]:
    pdf_dir = output_dir / "pdfs"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    results: list[PdfResult] = []
    context = page.context
    for index, row in enumerate([row for row in rows if row.get("report_url")], start=1):
        incident_id = str(row.get("incident_id") or "")
        report_id = str(row.get("report_id") or "")
        url = str(row["report_url"])
        print(f"download {index}: incident {incident_id} report {report_id}")
        try:
            response = context.request.get(
                url,
                headers={"Accept": "application/pdf,text/html,*/*", "Referer": str(row.get("source_url") or page.url)},
                timeout=30000,
            )
            body = response.body()
            content_type = response.headers.get("content-type", "")
            if "pdf" in content_type.lower() or body.startswith(b"%PDF"):
                output_path = pdf_dir / f"{safe_filename(f'{incident_id}_{report_id}')}.pdf"
                output_path.write_bytes(body)
                results.append(
                    PdfResult(
                        incident_id=incident_id,
                        report_id=report_id,
                        report_url=url,
                        ok=True,
                        status=response.status,
                        content_type=content_type,
                        output_path=str(output_path),
                    )
                )
            elif content_has_challenge(body):
                results.append(
                    PdfResult(
                        incident_id=incident_id,
                        report_id=report_id,
                        report_url=url,
                        ok=False,
                        status=response.status,
                        content_type=content_type,
                        reason="challenge_or_validation_page",
                    )
                )
            else:
                html_path = pdf_dir / f"{safe_filename(f'{incident_id}_{report_id}')}.html"
                html_path.write_bytes(body)
                results.append(
                    PdfResult(
                        incident_id=incident_id,
                        report_id=report_id,
                        report_url=url,
                        ok=False,
                        status=response.status,
                        content_type=content_type,
                        output_path=str(html_path),
                        reason="not_pdf_saved_html",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - keep the batch moving.
            results.append(
                PdfResult(
                    incident_id=incident_id,
                    report_id=report_id,
                    report_url=url,
                    ok=False,
                    status=None,
                    content_type="",
                    reason=f"{type(exc).__name__}: {exc}",
                )
            )
        page.wait_for_timeout(delay_ms)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect North Dakota HazConnect incidents through an existing CDP Chrome tab.")
    parser.add_argument("--cdp", default="http://127.0.0.1:9222", help="Chrome DevTools endpoint.")
    parser.add_argument("--url-match", default=MATCH_URL, help="Substring used to find the open HazConnect tab.")
    parser.add_argument("--output-dir", type=Path, default=Path("research/results/hazconnect"))
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--page-delay-ms", type=int, default=900)
    parser.add_argument("--pdf-delay-ms", type=int, default=1200)
    parser.add_argument("--download-pdfs", action="store_true")
    args = parser.parse_args()

    try:
      from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "Playwright is required for CDP attachment. Run with:\n"
            "  uv run --with playwright python research/hazconnect_cdp_collect.py ..."
        ) from exc

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(args.cdp)
        page = find_hazconnect_page(browser, args.url_match)
        page.wait_for_load_state("domcontentloaded")
        print(f"attached: {page.url}")
        rows = collect_pages(page, max_pages=args.max_pages, delay_ms=args.page_delay_ms)
        json_path, csv_path = write_rows(rows, args.output_dir)
        print(f"wrote {len(rows)} rows to {json_path} and {csv_path}")

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "attached_url": page.url,
            "row_count": len(rows),
            "json_path": str(json_path),
            "csv_path": str(csv_path),
            "pdf_results": [],
        }
        if args.download_pdfs:
            pdf_results = download_pdfs(page, rows, args.output_dir, delay_ms=args.pdf_delay_ms)
            report["pdf_results"] = [asdict(result) for result in pdf_results]
            ok_count = sum(1 for result in pdf_results if result.ok)
            print(f"pdf pass: {ok_count}/{len(pdf_results)} saved as PDF")

        report_path = args.output_dir / "hazconnect_run_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"wrote run report to {report_path}")
        browser.close()


if __name__ == "__main__":
    main()
