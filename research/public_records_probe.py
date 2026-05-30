from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)

CHALLENGE_PATTERNS = {
    "captcha": re.compile(r"\bcaptcha\b|g-recaptcha|hcaptcha|turnstile", re.I),
    "cloudflare_challenge": re.compile(r"just a moment|cf_chl|challenge-platform|cloudflare", re.I),
    "aws_waf_challenge": re.compile(r"awswaf|verify that you're not a robot", re.I),
    "incapsula": re.compile(r"incapsula|_Incapsula_Resource", re.I),
}


@dataclass
class ProbeResult:
    name: str
    state: str
    source_type: str
    url: str
    ok: bool = False
    status_code: int | None = None
    challenge_markers: list[str] = field(default_factory=list)
    row_count: int | None = None
    sample: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "dnt": "1",
            "sec-gpc": "1",
            "user-agent": USER_AGENT,
        }
    )
    return session


def challenge_markers(text: str) -> list[str]:
    return [name for name, pattern in CHALLENGE_PATTERNS.items() if pattern.search(text)]


def first_items(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return rows[:limit]


def parse_well_table(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", attrs={"summary": re.compile("Well Log search results", re.I)})
    if table is None:
        return []

    headers = [cell.get_text(" ", strip=True) for cell in table.find_all("th")]
    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr"):
        cells = [cell.get_text(" ", strip=True).replace("\xa0", " ") for cell in tr.find_all("td")]
        if not cells:
            continue
        if len(cells) != len(headers):
            continue
        rows.append({headers[i]: cells[i].strip() for i in range(len(headers))})
    return rows


def probe_nd_business(sample_limit: int) -> ProbeResult:
    url = "https://firststop.sos.nd.gov/api/Records/businesssearch"
    result = ProbeResult(
        name="North Dakota FirstStop business search",
        state="ND",
        source_type="company_listing",
        url=url,
    )

    session = make_session()
    landing = session.get("https://firststop.sos.nd.gov/search/business", timeout=30)
    result.challenge_markers.extend(challenge_markers(landing.text))
    if result.challenge_markers:
        result.status_code = landing.status_code
        result.notes.append("Landing page contains challenge markers.")
        return result

    payload = {
        "SEARCH_VALUE": "DEVON ENERGY WILLISTON",
        "STARTS_WITH_YN": False,
        "ACTIVE_ONLY_YN": False,
    }
    response = session.post(
        url,
        json=payload,
        headers={
            "authorization": "undefined",
            "content-type": "application/json",
            "origin": "https://firststop.sos.nd.gov",
            "referer": "https://firststop.sos.nd.gov/search/business",
        },
        timeout=30,
    )
    result.status_code = response.status_code
    result.challenge_markers.extend(marker for marker in challenge_markers(response.text) if marker not in result.challenge_markers)
    response.raise_for_status()

    data = response.json()
    rows = sorted(data.get("rows", {}).values(), key=lambda row: row.get("SORT_INDEX", 0))
    normalized_rows = [
        {
            "id": row.get("ID"),
            "record_number": row.get("RECORD_NUM"),
            "title": " | ".join(row.get("TITLE") or []),
            "status": row.get("STATUS"),
            "standing": row.get("STANDING"),
            "filing_date": row.get("FILING_DATE"),
        }
        for row in rows
    ]

    if normalized_rows:
        first = normalized_rows[0]
        detail = session.get(
            f"https://firststop.sos.nd.gov/api/FilingDetail/business/{first['id']}/false",
            headers={"authorization": "undefined", "referer": "https://firststop.sos.nd.gov/search/business"},
            timeout=30,
        )
        detail.raise_for_status()
        detail_data = detail.json()
        detail_pairs = {
            item.get("LABEL"): item.get("VALUE")
            for item in detail_data.get("DRAWER_DETAIL_LIST", [])
            if item.get("LABEL") and item.get("VALUE")
        }
        first["detail"] = {
            key: detail_pairs.get(key)
            for key in ["Filing Type", "Status", "Formed In", "Principal Address", "Commercial Registered Agent"]
            if key in detail_pairs
        }

        history = session.get(
            f"https://firststop.sos.nd.gov/api/History/business/{first['record_number']}",
            headers={"authorization": "undefined", "referer": "https://firststop.sos.nd.gov/search/business"},
            timeout=30,
        )
        history.raise_for_status()
        history_data = history.json()
        first["history_sample"] = first_items(history_data.get("AMENDMENT_LIST", []), 3)
        result.notes.append(
            f"Detail and history endpoints also worked for record {first['record_number']}."
        )

    result.ok = response.ok and bool(normalized_rows) and not result.challenge_markers
    result.row_count = len(normalized_rows)
    result.sample = first_items(normalized_rows, sample_limit)
    return result


def probe_nd_oilgas_operator(sample_limit: int) -> ProbeResult:
    return probe_nd_oilgas(
        name="North Dakota DMR oil/gas wells by operator",
        payload={
            "VTI-GROUP": "0",
            "ddmOperator": "WPX ENERGY WILLISTON, LLC",
            "ddmField": " ",
            "ddmSection": "0",
            "ddmTownship": "0",
            "ddmRange": "0",
            "B1": "Submit",
        },
        sample_limit=sample_limit,
    )


def probe_nd_oilgas_township(sample_limit: int) -> ProbeResult:
    return probe_nd_oilgas(
        name="North Dakota DMR oil/gas wells by township",
        payload={
            "VTI-GROUP": "0",
            "ddmOperator": " ",
            "ddmField": " ",
            "ddmSection": "0",
            "ddmTownship": "133",
            "ddmRange": "0",
            "B1": "Submit",
        },
        sample_limit=sample_limit,
    )


def probe_nd_oilgas(name: str, payload: dict[str, str], sample_limit: int) -> ProbeResult:
    url = "https://www.dmr.nd.gov/oilgas/findwellsvw.asp"
    result = ProbeResult(name=name, state="ND", source_type="oil_gas_wells", url=url)
    session = make_session()

    landing = session.get(url, timeout=30)
    result.challenge_markers.extend(challenge_markers(landing.text))
    if result.challenge_markers:
        result.status_code = landing.status_code
        result.notes.append("Landing page contains challenge markers.")
        return result

    soup = BeautifulSoup(landing.text, "html.parser")
    operator_options = soup.select("#ddmOperator option")
    result.notes.append(f"Landing form exposed {max(len(operator_options) - 1, 0)} operator options.")

    response = session.post(
        url,
        data=payload,
        headers={
            "content-type": "application/x-www-form-urlencoded",
            "origin": "https://www.dmr.nd.gov",
            "referer": url,
        },
        timeout=30,
    )
    result.status_code = response.status_code
    result.challenge_markers.extend(marker for marker in challenge_markers(response.text) if marker not in result.challenge_markers)
    response.raise_for_status()

    rows = parse_well_table(response.text)
    result.ok = response.ok and bool(rows) and not result.challenge_markers
    result.row_count = len(rows)
    result.sample = first_items(rows, sample_limit)
    return result


def probe_hennepin_property(sample_limit: int) -> ProbeResult:
    url = "https://gis.hennepin.us/arcgis/rest/services/HennepinData/LAND_PROPERTY/MapServer/1/query"
    result = ProbeResult(
        name="Hennepin County parcel/property ArcGIS query",
        state="MN",
        source_type="property_records",
        url=url,
    )
    session = make_session()
    params = {
        "f": "json",
        "where": "1=1",
        "outFields": "PID,OWNER_NM,TAXPAYER_NM,HOUSE_NO,STREET_NM,MUNIC_NM,MKT_VAL_TOT,TAX_TOT",
        "returnGeometry": "false",
        "resultRecordCount": sample_limit,
    }
    response = session.get(url, params=params, timeout=30)
    result.status_code = response.status_code
    result.challenge_markers = challenge_markers(response.text)
    response.raise_for_status()
    data = response.json()
    rows = [feature.get("attributes", {}) for feature in data.get("features", [])]
    cleaned = [{key: value.strip() if isinstance(value, str) else value for key, value in row.items()} for row in rows]
    result.ok = response.ok and bool(cleaned) and not result.challenge_markers
    result.row_count = len(cleaned)
    result.sample = first_items(cleaned, sample_limit)
    result.notes.append("ArcGIS FeatureServer query returned owner, taxpayer, address, valuation, and tax fields.")
    return result


PROBES = {
    "nd-business": probe_nd_business,
    "nd-oilgas-operator": probe_nd_oilgas_operator,
    "nd-oilgas-township": probe_nd_oilgas_township,
    "mn-hennepin-property": probe_hennepin_property,
}


def run_probe(name: str, sample_limit: int) -> ProbeResult:
    try:
        return PROBES[name](sample_limit)
    except Exception as exc:  # noqa: BLE001 - probe failures should become report data.
        return ProbeResult(
            name=name,
            state="unknown",
            source_type="unknown",
            url="",
            ok=False,
            notes=[f"{type(exc).__name__}: {exc}"],
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify public-record endpoints for agentic research demos.")
    parser.add_argument(
        "--probe",
        choices=sorted(PROBES),
        action="append",
        help="Run one probe. Repeat to run multiple. Defaults to all probes.",
    )
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("research/results/public_records_probe_latest.json"))
    args = parser.parse_args()

    selected = args.probe or sorted(PROBES)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "probe_names": selected,
        "results": [asdict(run_probe(name, args.sample_limit)) for name in selected],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
