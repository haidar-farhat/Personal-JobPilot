"""Ashby career-page scanner via the public posting-api endpoint.

Pattern adapted from career-ops `scan.mjs` (zero-token ATS scanning).
Endpoint: https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

import logging
from pathlib import Path

import yaml

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)


class AshbyScanner(BaseScanner):
    """Scan Ashby-based career pages via their public posting API."""

    source_name = "ashby"
    rate_limit_seconds = 1.5

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies_config = self._load_companies()

    def _load_companies(self) -> list[dict]:
        config_path = Path(__file__).parent.parent.parent / "config" / "target_companies.yaml"
        if not config_path.exists():
            return []
        with open(config_path) as f:
            data = yaml.safe_load(f)
        return [
            c for c in data.get("companies", [])
            if c.get("ats_platform") == "ashby" and c.get("ashby_slug")
        ]

    def scan(self) -> list[RawJob]:
        jobs: list[RawJob] = []
        for company in self.companies_config:
            try:
                company_jobs = self._scan_company(company)
                jobs.extend(company_jobs)
                logger.info(f"[ashby] {company['name']}: found {len(company_jobs)} relevant jobs")
            except Exception as e:
                logger.error(f"[ashby] Error scanning {company['name']}: {e}")
        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        slug = company["ashby_slug"]
        api_url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true"
        response = self._safe_request(api_url)
        if not response:
            return []

        try:
            data = response.json()
        except Exception:
            logger.error(f"[ashby] Failed to parse JSON from {api_url}")
            return []

        postings = data.get("jobs", [])
        if not isinstance(postings, list):
            return []

        relevant: list[RawJob] = []
        for job_data in postings:
            title = job_data.get("title", "")
            title_lower = title.lower()
            if not self._title_is_relevant(title):
                continue

            location = job_data.get("location", "") or ""
            if isinstance(location, dict):
                location = location.get("name", "")
            location_lower = (location or "").lower()
            is_remote_field = bool(job_data.get("isRemote", False))
            # ponytail: no Bay Area allow-list — see the note in GreenhouseScanner.
            # Geography is decided by BaseScanner's non-US gate plus the ranker's
            # location_remote dimension, both config-driven.

            url = job_data.get("jobUrl") or job_data.get("applyUrl") or ""
            if not url:
                continue

            description = job_data.get("descriptionPlain") or ""
            if not description and job_data.get("descriptionHtml"):
                # Strip simple HTML if only the html field was returned
                from bs4 import BeautifulSoup
                description = BeautifulSoup(job_data["descriptionHtml"], "lxml").get_text(
                    separator="\n", strip=True
                )

            comp_text = ""
            comp = job_data.get("compensation") or {}
            if isinstance(comp, dict):
                summary = comp.get("compensationTierSummary") or comp.get("summary")
                if summary:
                    comp_text = str(summary)

            relevant.append(RawJob(
                title=title,
                company=company["name"],
                location=location or company.get("location", ""),
                url=url,
                source=f"ashby:{slug}",
                source_id=str(job_data.get("id", "")),
                description=description[:5000],
                salary_text=comp_text,
                is_remote=is_remote_field or "remote" in location_lower or "remote" in title_lower,
            ))

        return relevant
