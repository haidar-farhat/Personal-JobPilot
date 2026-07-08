"""Import jobs from external agents (MCP job-board connectors) into the pipeline.

Claude sessions have access to job-board MCP servers (Indeed today; others as
they appear). Those run on the agent side, not in this process — so the bridge
is an API: the agent searches the MCP, normalizes the results, and POSTs them
here. Imported jobs flow through the SAME machinery as scanned jobs (excluded-
title filter, dedupe/merge, comp parsing, Application record, ScanLog) and are
scored by the ranker immediately, so they show up in the dashboard with a
match %% like any scanner find.

POST /api/import/jobs
{
  "source": "mcp-indeed",
  "jobs": [{"title": "...", "company": "...", "location": "...", "url": "...",
            "description": "", "salary_text": "$50 - $100 an hour",
            "date_posted": "June 11, 2026", "job_type": "Part-time"}]
}
"""

import logging
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter
from pydantic import BaseModel, Field

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/import", tags=["import"])

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _settings() -> dict:
    with open(PROJECT_ROOT / "config" / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class ImportedJob(BaseModel):
    title: str
    company: str
    url: str
    location: str = ""
    description: str = ""
    salary_text: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    source_id: str = ""
    date_posted: str = ""          # ISO "2026-06-11" or "June 11, 2026"
    job_type: str = ""             # merged into description for comp parsing
    is_remote: bool = False


class JobImportPayload(BaseModel):
    source: str = Field(default="mcp-import", max_length=40)
    jobs: list[ImportedJob]
    # LLM scoring is slow (Ollama) — tests and bulk loads can defer it to the
    # watchdog's next ranking cycle instead of blocking the request
    score: bool = True


def _parse_posted(s: str) -> datetime | None:
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


class _AgentImportScanner(BaseScanner):
    """A scanner whose scan() returns jobs handed to us by the agent —
    everything downstream (filter, dedupe, store, log) is inherited."""

    def __init__(self, config: dict, jobs: list[RawJob], source: str):
        super().__init__(config)
        self._jobs = jobs
        self.source_name = source

    def scan(self) -> list[RawJob]:
        return self._jobs


@router.post("/jobs")
def import_jobs(payload: JobImportPayload):
    raw = []
    for j in payload.jobs:
        # job_type ("Part-time", "$X an hour") helps parse_employment_type /
        # parse_hourly downstream when the description is thin
        desc = j.description or ""
        if j.job_type and j.job_type.lower() not in desc.lower():
            desc = f"{j.job_type}. {desc}".strip()
        raw.append(RawJob(
            title=j.title.strip(),
            company=j.company.strip(),
            location=(j.location or "").strip(),
            url=j.url,
            source=payload.source,
            description=desc,
            salary_min=j.salary_min,
            salary_max=j.salary_max,
            salary_text=j.salary_text,
            source_id=j.source_id,
            date_posted=_parse_posted(j.date_posted),
            is_remote=j.is_remote or "remote" in (j.location or "").lower(),
        ))

    config = _settings()
    result = _AgentImportScanner(config, raw, payload.source).run()

    # score the newcomers right away so the dashboard shows a match %
    scored = 0
    if payload.score and result.get("jobs_new"):
        try:
            from agents.ranker import rank_new_jobs
            rank = rank_new_jobs(config)
            scored = rank.get("scored", rank.get("jobs_scored", 0)) or 0
        except Exception as e:  # scoring is best-effort; imports must not fail
            logger.error(f"[import] ranking failed: {e}")

    return {
        "ok": True,
        "source": payload.source,
        "received": len(payload.jobs),
        "imported": result.get("jobs_new", 0),
        "duplicates_merged": result.get("jobs_found", 0) - result.get("jobs_new", 0)
                             - len(result.get("errors", [])),
        "scored": scored,
        "errors": result.get("errors", []),
    }
