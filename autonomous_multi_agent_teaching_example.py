from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from textwrap import dedent
from typing import Literal
from urllib.parse import urljoin

import requests
from dotenv import load_dotenv
from markdownify import markdownify as md
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.capabilities import WebFetch, WebSearch
from pydantic_ai.exceptions import UnexpectedModelBehavior


MODEL = "openrouter:anthropic/claude-haiku-4-5"
MEETING_TEXT_PATH = Path("20250715 BZA min.txt")
OUTPUT_PATH = Path("outputs/autonomous_research_queues.json")
ASSESSMENT_URL = "https://apps.spotsylvaniacountyva.gov/assessment/assessment/results.cfm"
ASSESSMENT_BASE_URL = "https://apps.spotsylvaniacountyva.gov/assessment/assessment/"
ASSESSMENT_DETAIL_RE = re.compile(r"(?:https?://[^\s\])\"<>]+)?Info\.cfm\?OID=[A-Za-z0-9]+", re.IGNORECASE)

ITEMS_PER_CASE_PER_ROUND = 3
MAX_RESEARCH_ROUNDS = 4

load_dotenv()
if not os.getenv("OPENROUTER_API_KEY"):
    raise RuntimeError("OPENROUTER_API_KEY is required.")

from braintrust.wrappers.pydantic_ai import setup_pydantic_ai

if os.getenv("BRAINTRUST_API_KEY"):
    setup_pydantic_ai(project_name="dataharvest-2026")
    print("Braintrust connection established.")
else:
    print("Set BRAINTRUST_API_KEY to enable Braintrust tracing.")

class Location(BaseModel):
    address: str | None = None
    parcel_id: str | None = None


class ExtractedCase(BaseModel):
    case_id: str = Field(description="Example: V25-0002")
    request: str
    people: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    public_conflict: str | None = None
    outcome: str | None = None


class MeetingExtraction(BaseModel):
    cases: list[ExtractedCase]
    summary: str


class ResearchItem(BaseModel):
    kind: Literal["person", "company", "location"]
    name: str | None = Field(default=None, description="Use for person or company research.")
    location: Location | None = Field(default=None, description="Use for location research.")
    reason: str


class ResearchFindings(BaseModel):
    summary: str
    proposed_research: list[ResearchItem] = Field(default_factory=list)
    source_ledger: list[str] = Field(default_factory=list)


class ResearchRecord(BaseModel):
    item: ResearchItem
    summary: str
    source_ledger: list[str] = Field(default_factory=list)
    proposed_research: list[ResearchItem] = Field(default_factory=list)


class CaseSynthesis(BaseModel):
    case_id: str
    synthesis: str
    important_findings: list[str] = Field(default_factory=list)
    unresolved_leads: list[str] = Field(default_factory=list)
    source_ledger: list[str] = Field(default_factory=list)


@dataclass
class CaseResearch:
    case: ExtractedCase
    queue: list[ResearchItem] = field(default_factory=list)
    seen: set[str] = field(default_factory=set)
    results: list[ResearchRecord] = field(default_factory=list)


def _assessment_page_to_markdown(html: str, source_url: str) -> str:
    html = html.replace("Info.cfm?", f"{ASSESSMENT_BASE_URL}Info.cfm?")
    content = md(html, strip=["img", "script", "style"]).strip()
    return f"Source URL: {source_url}\n\n{content}"


def _post_assessment_search(data: dict[str, str]) -> str:
    try:
        response = requests.post(ASSESSMENT_URL, data=data, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        return f"Assessment search failed for {data}: {error}"
    return _assessment_page_to_markdown(response.text, ASSESSMENT_URL)


def search_assessments_by_parcel_id(parcel_id: str) -> str:
    """
    Search for assessments related to a given parcel ID.
    If no record is found, try the county site's trailing-hyphen parcel format.
    """
    data = {
        "streetnum": "",
        "streetname": "",
        "parcelid": parcel_id,
        "submitsearch": "Go",
    }
    result = _post_assessment_search(data)

    if "Info.cfm?OID=" not in result and not parcel_id.endswith("-"):
        data["parcelid"] = f"{parcel_id}-"
        retry_result = _post_assessment_search(data)
        if "Info.cfm?OID=" in retry_result:
            return (
                f"No record was found for parcel ID {parcel_id!r}. "
                f"The county site matched {data['parcelid']!r} instead.\n\n"
                f"{retry_result}"
            )

    return result


def search_assessments_by_address(street_num: str, street_name: str) -> str:
    """
    Search for assessments related to a given street address.
    Street name should be only the name and must NOT include RD, DR, ST, AVE.
    """
    data = {
        "streetnum": street_num,
        "streetname": street_name,
        "parcelid": "",
        "submitsearch": "Go",
    }
    return _post_assessment_search(data)


def fetch_assessment_detail(url: str) -> str:
    """
    Fetch a county assessment detail page from a Details or Info.cfm link.
    Accepts either a full URL or a relative Info.cfm link from the search page.
    """
    match = ASSESSMENT_DETAIL_RE.search(url)
    detail_url = urljoin(ASSESSMENT_BASE_URL, match.group(0) if match else url.strip())
    try:
        response = requests.get(detail_url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        return f"Assessment detail fetch failed for {detail_url}: {error}"
    return _assessment_page_to_markdown(response.text, detail_url)


meeting_agent = Agent(
    MODEL,
    output_type=MeetingExtraction,
    instructions="""
Extract all zoning cases from the meeting minutes.
For each case, separate people, companies/organizations, and locations.
A location may include a street address, a parcel ID, or both.
Do not include routine board members, county staff, or elected officials unless
they are directly involved in the case as an applicant, owner, attorney,
neighbor, contractor, business operator, or substantive public speaker.
Do not research anything. Only extract what is in the meeting text.
""",
)

location_agent = Agent(
    MODEL,
    output_type=ResearchFindings,
    tool_retries=2,
    tools=[
        search_assessments_by_parcel_id,
        search_assessments_by_address,
        fetch_assessment_detail,
    ],
    instructions="""
Research one location for one zoning case.
Use the county assessment search tools. If a search result includes an
Info.cfm assessment link, call fetch_assessment_detail with that link.
Summarize the property, owner, mailing address, parcel ID, and assessment details.
The summary is what you learned from this research step, using the tools and
web pages you inspected. Do not merely restate the case description.
Return proposed_research for clearly relevant people, companies, or locations
that should be researched next. Keep proposed locations to the best few leads.
Only call fetch_assessment_detail on Details or Info.cfm links returned by the
assessment search tools.
""",
)

person_agent = Agent(
    MODEL,
    output_type=ResearchFindings,
    tool_retries=2,
    capabilities=[WebSearch(native=False, local=True), WebFetch(native=False, local=True)],
    instructions="""
Research one person for one zoning case.
Use web search and web fetch. Look for context relevant to Spotsylvania County
zoning, property ownership, business connections, public records, or the case.
The summary is what you learned from this research step, using the search
results and web pages you inspected. Do not merely restate the case description.
Return proposed_research for clearly relevant people, companies, or locations
that should be researched next. Do not return long address dumps.
Only fetch URLs returned by the search tool, user input, or a source ledger.
""",
)

company_agent = Agent(
    MODEL,
    output_type=ResearchFindings,
    tool_retries=2,
    capabilities=[WebSearch(native=False, local=True), WebFetch(native=False, local=True)],
    instructions="""
Research one company or organization for one zoning case.
Use web search and web fetch. Look for registered agents, officers, related
properties, or case context. Keep companies separate from people.
The summary is what you learned from this research step, using the search
results and web pages you inspected. Do not merely restate the case description.
Return proposed_research for clearly relevant people, companies, or locations
that should be researched next.
Only fetch URLs returned by the search tool, user input, or a source ledger.
""",
)

synthesis_agent = Agent(
    MODEL,
    output_type=CaseSynthesis,
    instructions="""
Synthesize the research for one zoning case.
Use the extracted case and detailed research records. Explain what was learned,
what sources support it, and what leads remain unresolved because the queue
stopped at its limits. Do not invent facts.
""",
)


def item_label(item: ResearchItem) -> str:
    if item.kind == "location" and item.location:
        pieces = [item.location.address, item.location.parcel_id]
        return " / ".join(piece for piece in pieces if piece)
    return item.name or ""


def add_to_queue(case_research: CaseResearch, item: ResearchItem) -> bool:
    label = item_label(item)
    key = f"{item.kind}:{label.lower()}" if label else ""
    if not key or key in case_research.seen:
        return False
    case_research.queue.append(item)
    case_research.seen.add(key)
    return True


async def research_item(case_research: CaseResearch, item: ResearchItem) -> ResearchRecord:
    prompt = dedent("""
        Case:
        {case}

        Research item:
        {item}
    """).format(
        case=case_research.case.model_dump_json(indent=2),
        item=item.model_dump_json(indent=2),
    )

    try:
        if item.kind == "location":
            response = await location_agent.run(prompt)
        elif item.kind == "person":
            response = await person_agent.run(prompt)
        else:
            response = await company_agent.run(prompt)
    except UnexpectedModelBehavior as error:
        return ResearchRecord(
            item=item,
            summary=f"Research step failed before completion: {error}",
            source_ledger=["Pydantic AI tool retry limit reached during this research step."],
            proposed_research=[],
        )

    findings = response.output
    return ResearchRecord(
        item=item,
        summary=findings.summary,
        source_ledger=findings.source_ledger,
        proposed_research=findings.proposed_research,
    )


async def main() -> None:
    print("1. Extracting cases from the meeting text...")
    meeting_text = MEETING_TEXT_PATH.read_text(encoding="utf-8")
    extraction = await meeting_agent.run(meeting_text)

    cases = {}
    for case in extraction.output.cases:
        case_research = CaseResearch(case=case)
        cases[case.case_id] = case_research

        for location in case.locations:
            add_to_queue(
                case_research,
                ResearchItem(kind="location", location=location, reason="Named in meeting minutes"),
            )
        for person in case.people:
            add_to_queue(
                case_research,
                ResearchItem(kind="person", name=person, reason="Named in meeting minutes"),
            )
        for company in case.companies:
            add_to_queue(
                case_research,
                ResearchItem(kind="company", name=company, reason="Named in meeting minutes"),
            )

    print(f"   Extracted {len(cases)} cases.")
    print("\nInitial queues:")
    for case_id, case_research in cases.items():
        print(f"   {case_id}")
        for item in case_research.queue:
            print(f"      - {item.kind}: {item_label(item)}")

    print("\n2. Running per-case research queues...")
    print(f"   Stopping after {MAX_RESEARCH_ROUNDS} rounds.")
    round_number = 1
    while round_number <= MAX_RESEARCH_ROUNDS:
        batch = []

        for case_id, case_research in cases.items():
            for _ in range(ITEMS_PER_CASE_PER_ROUND):
                if not case_research.queue:
                    break
                batch.append({
                    "case_id": case_id,
                    "item": case_research.queue.pop(0),
                })

        if not batch:
            break

        print(f"\nRound {round_number}: {len(batch)} parallel research job(s)")
        for job in batch:
            item = job["item"]
            print(f"   researching {job['case_id']} {item.kind}: {item_label(item)}")

        records = await asyncio.gather(
            *[
                research_item(cases[job["case_id"]], job["item"])
                for job in batch
            ]
        )

        for job, record in zip(batch, records):
            case_id = job["case_id"]
            case_research = cases[case_id]
            case_research.results.append(record)

            print(f"   result for {case_id} {record.item.kind}: {item_label(record.item)}")
            print(f"      summary: {record.summary}")

            if record.proposed_research:
                print("      proposed research:")
            else:
                print("      proposed research: none")

            for proposed in record.proposed_research:
                if add_to_queue(case_research, proposed):
                    print(f"         queued {proposed.kind}: {item_label(proposed)}")
                else:
                    label = item_label(proposed)
                    key = f"{proposed.kind}:{label.lower()}" if label else ""
                    queued = {
                        f"{item.kind}:{item_label(item).lower()}"
                        for item in case_research.queue
                    }
                    researched = {
                        f"{result.item.kind}:{item_label(result.item).lower()}"
                        for result in case_research.results
                    }
                    if key in queued:
                        print(f"         {label} (skipped, already in queue)")
                    elif key in researched:
                        print(f"         {label} (skipped, already researched)")
                    else:
                        print(f"         {label} (skipped)")

        print("   queues after round:")
        for case_id, case_research in cases.items():
            labels = [
                f"{item.kind}: {item_label(item)}"
                for item in case_research.queue
            ]
            print(f"      {case_id}: {labels or 'empty'}")

        round_number += 1

    print("\n3. Synthesizing each case...")
    syntheses = await asyncio.gather(
        *[
            synthesis_agent.run(dedent("""
                Extracted case:
                {case}

                Detailed research:
                {research}

                Unresolved leads still in this case's queue:
                {unresolved}

                Research stopped after {rounds} rounds.
            """).format(
                case=case_research.case.model_dump_json(indent=2),
                research=json.dumps([record.model_dump() for record in case_research.results], indent=2),
                unresolved=json.dumps([item.model_dump() for item in case_research.queue], indent=2),
                rounds=MAX_RESEARCH_ROUNDS,
            ))
            for case_research in cases.values()
        ]
    )

    print()
    for (case_id, case_research), synthesis_response in zip(cases.items(), syntheses):
        synthesis = synthesis_response.output
        print(f"   {case_id}: {synthesis.synthesis}")

        if synthesis.important_findings:
            print("      useful research:")
            for finding in synthesis.important_findings:
                print(f"      - {finding}")
        elif case_research.results:
            print("      research completed, but no key findings were highlighted")
        else:
            print("      no research results")

    output = {
        "meeting_summary": extraction.output.summary,
        "cases": {
            case_id: {
                "case": case_research.case.model_dump(),
                "remaining_queue": [item.model_dump() for item in case_research.queue],
                "research_results": [record.model_dump() for record in case_research.results],
                "synthesis": synthesis.output.model_dump(),
            }
            for (case_id, case_research), synthesis in zip(cases.items(), syntheses)
        },
    }

    OUTPUT_PATH.parent.mkdir(exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
