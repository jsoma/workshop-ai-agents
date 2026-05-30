# Public Records Probe Scripts

This folder contains small verification scripts for public-record sources that are useful in an agentic research demo.

Run all built-in probes:

```bash
uv run python research/public_records_probe.py
```

Run only the North Dakota company and oil/gas probes:

```bash
uv run python research/public_records_probe.py --probe nd-business --probe nd-oilgas-operator --probe nd-oilgas-township
```

The script writes JSON evidence to `research/results/public_records_probe_latest.json`. It does not use copied browser cookies, does not solve CAPTCHAs, and marks challenge pages when it sees common CAPTCHA, Cloudflare, AWS WAF, or Incapsula markers.

Current built-in probes:

- `nd-business`: North Dakota FirstStop business search, filing detail, and filing history JSON endpoints.
- `nd-oilgas-operator`: North Dakota DMR oil/gas well list by operator via HTML form POST.
- `nd-oilgas-township`: North Dakota DMR oil/gas well list by township via HTML form POST.
- `mn-hennepin-property`: Hennepin County, Minnesota parcel/property ArcGIS query with owner and valuation fields.

## North Dakota HazConnect

`hazconnect_tampermonkey.user.js` is a Tampermonkey userscript for an already-open HazConnect incident table. It adds a small panel that can collect the current page, click through all pages using the page's own Next control, export CSV/JSON, and download report PDFs that the browser session can access.

`hazconnect_cdp_collect.py` does the same collection through Chrome DevTools Protocol when Chrome has been started with a remote debugging port:

```bash
uv run --with playwright python research/hazconnect_cdp_collect.py --download-pdfs
```

If your normal Chrome is already running without CDP enabled, launch a separate debug profile and open the HazConnect page there:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome \
  --remote-debugging-port=9222 \
  --user-data-dir=/tmp/chrome-hazconnect-cdp
```

The CDP script writes `research/results/hazconnect/hazconnect_incidents.json`, `hazconnect_incidents.csv`, downloaded files under `research/results/hazconnect/pdfs/`, and `hazconnect_run_report.json`. It does not solve or bypass CAPTCHA; it only uses pages and report URLs that the browser session can already access.
