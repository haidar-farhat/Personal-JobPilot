"""Career page scanners for Greenhouse, Lever, and custom company pages."""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)

def _description_cap(config: dict | None = None) -> int:
    """How much job description to keep. settings.yaml `scanner.description_max_chars`.

    Was hard-coded per-file (5000 here, 8000 in company_sites). The description
    is the only material the tailor and ranker have to work with, so a tight cap
    silently degrades every downstream stage.
    """
    return int(((config or {}).get("scanner") or {}).get("description_max_chars", 20000))



class GreenhouseScanner(BaseScanner):
    """Scan Greenhouse-based career pages via their public JSON API."""

    source_name = "greenhouse"
    rate_limit_seconds = 1.5

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies_config = self._load_companies()

    def _load_companies(self) -> list[dict]:
        """Load Greenhouse companies from target_companies.yaml."""
        from pathlib import Path
        import yaml

        config_path = Path(__file__).parent.parent.parent / "config" / "target_companies.yaml"
        if not config_path.exists():
            return []

        with open(config_path) as f:
            data = yaml.safe_load(f)

        return [c for c in data.get("companies", []) if c.get("ats_platform") == "greenhouse" and c.get("api_url")]

    def scan(self) -> list[RawJob]:
        jobs = []

        for company in self.companies_config:
            try:
                company_jobs = self._scan_company(company)
                jobs.extend(company_jobs)
                logger.info(f"[greenhouse] {company['name']}: found {len(company_jobs)} relevant jobs")
            except Exception as e:
                logger.error(f"[greenhouse] Error scanning {company['name']}: {e}")

        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        """Scan a single Greenhouse company's job listings."""
        api_url = company["api_url"]
        response = self._safe_request(api_url)
        if not response:
            return []

        try:
            data = response.json()
        except Exception:
            logger.error(f"[greenhouse] Failed to parse JSON from {api_url}")
            return []

        jobs_data = data.get("jobs", data) if isinstance(data, dict) else data
        if not isinstance(jobs_data, list):
            return []

        relevant_jobs = []
        search_keywords_lower = [kw.lower() for kw in self.keywords]

        for job_data in jobs_data:
            title = job_data.get("title", "")
            title_lower = title.lower()

            # Check if title matches any configured/legacy relevance term
            if not self._title_is_relevant(title):
                continue

            # Extract location
            location = ""
            if "location" in job_data:
                loc = job_data["location"]
                if isinstance(loc, dict):
                    location = loc.get("name", "")
                else:
                    location = str(loc)

            location_lower = location.lower()

            # ponytail: no location allow-list here. Until 2026-08-03 this dropped
            # anything outside a hardcoded Bay Area keyword list, which silently
            # threw away every out-of-metro role — the relocation-track companies
            # in target_companies.yaml would never have produced a single onsite
            # job. Geography is now decided in two better places: BaseScanner's
            # non-US gate (run(), config-driven) and the ranker's location_remote
            # dimension, which grades US-remote 95 / target metro 85 / other
            # affordable US 65 / high-cost metro 40. A hard filter here would just
            # hide roles the ranker is already equipped to rank down.

            # Get job URL
            url = job_data.get("absolute_url", job_data.get("url", ""))
            if not url:
                continue

            # Get description
            content = job_data.get("content", "")
            if content:
                soup = BeautifulSoup(content, "lxml")
                description = soup.get_text(separator="\n", strip=True)
            else:
                description = ""

            relevant_jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location or company.get("location", ""),
                url=url,
                source=f"greenhouse:{company['name'].lower().replace(' ', '_')}",
                source_id=str(job_data.get("id", "")),
                description=description[:_description_cap(self.config)],
                is_remote="remote" in location_lower or "remote" in title_lower,
            ))

        return relevant_jobs


class LeverScanner(BaseScanner):
    """Scan Lever-based career pages via their public API."""

    source_name = "lever"
    rate_limit_seconds = 1.5

    def __init__(self, config: dict):
        super().__init__(config)
        self.companies_config = self._load_companies()

    def _load_companies(self) -> list[dict]:
        from pathlib import Path
        import yaml

        config_path = Path(__file__).parent.parent.parent / "config" / "target_companies.yaml"
        if not config_path.exists():
            return []

        with open(config_path) as f:
            data = yaml.safe_load(f)

        return [c for c in data.get("companies", []) if c.get("ats_platform") == "lever" and c.get("api_url")]

    def scan(self) -> list[RawJob]:
        jobs = []

        for company in self.companies_config:
            try:
                company_jobs = self._scan_company(company)
                jobs.extend(company_jobs)
                logger.info(f"[lever] {company['name']}: found {len(company_jobs)} relevant jobs")
            except Exception as e:
                logger.error(f"[lever] Error scanning {company['name']}: {e}")

        return jobs

    def _scan_company(self, company: dict) -> list[RawJob]:
        """Scan a single Lever company via the public JSON postings API.

        Until 2026-08-03 this fetched the configured api_url and parsed it as
        HTML looking for div.posting — but every Lever entry in
        target_companies.yaml points at api.lever.co/v0/postings/<slug>, which
        returns JSON. There are no div.posting elements in JSON, so this scanner
        returned zero jobs on every run since those entries were added. Now it
        normalises to the JSON endpoint and parses the documented fields.
        """
        api_url = company["api_url"].rstrip("/")

        # Accept either form in config: the JSON API or the human careers page.
        # jobs.lever.co/<slug> -> api.lever.co/v0/postings/<slug>
        if "jobs.lever.co" in api_url:
            slug = api_url.rsplit("/", 1)[-1]
            api_url = f"https://api.lever.co/v0/postings/{slug}"
        if "?" not in api_url:
            api_url += "?mode=json"

        response = self._safe_request(api_url)
        if not response:
            return []

        try:
            postings = response.json()
        except ValueError:
            logger.error(f"[lever] {company['name']}: non-JSON response from {api_url}")
            return []
        if not isinstance(postings, list):
            logger.error(f"[lever] {company['name']}: unexpected payload type {type(postings).__name__}")
            return []

        relevant_jobs = []
        for posting in postings:
            title = (posting.get("text") or "").strip()
            if not title or not self._title_is_relevant(title):
                continue

            url = posting.get("hostedUrl") or posting.get("applyUrl") or ""
            if not url:
                continue

            categories = posting.get("categories") or {}
            location = (categories.get("location") or company.get("location") or "").strip()

            # ponytail: no Bay Area allow-list — see the note in GreenhouseScanner.
            # The non-US gate in BaseScanner.run() plus the ranker's
            # location_remote dimension handle geography.

            workplace = (posting.get("workplaceType") or "").lower()
            title_lower = title.lower()

            relevant_jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location,
                url=url,
                source=f"lever:{company['name'].lower().replace(' ', '_')}",
                description=(posting.get("descriptionPlain") or "")[:6000],
                source_id=str(posting.get("id") or ""),
                is_remote=(
                    workplace == "remote"
                    or "remote" in location.lower()
                    or "remote" in title_lower
                ),
            ))

        return relevant_jobs
