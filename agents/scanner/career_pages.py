"""Career page scanners for Greenhouse, Lever, and custom company pages."""

import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from agents.scanner.base import BaseScanner, RawJob

logger = logging.getLogger(__name__)


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

            # Check if title matches any search keyword
            if not any(kw in title_lower for kw in ["data", "analyst", "scientist", "quantitative", "analytics", "business analyst"]):
                continue

            # Extract location
            location = ""
            if "location" in job_data:
                loc = job_data["location"]
                if isinstance(loc, dict):
                    location = loc.get("name", "")
                else:
                    location = str(loc)

            # Check if location is Bay Area related
            bay_area_keywords = ["san francisco", "oakland", "berkeley", "san jose", "bay area",
                                 "mountain view", "palo alto", "sunnyvale", "menlo park",
                                 "remote", "los gatos", "cupertino", "redwood city"]
            location_lower = location.lower()
            is_bay_area = any(kw in location_lower for kw in bay_area_keywords) or not location

            if not is_bay_area:
                continue

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
                description=description[:5000],  # Limit description length
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
        """Scan a single Lever company's job listings."""
        api_url = company["api_url"]
        # Lever's public API endpoint
        if not api_url.endswith("/"):
            api_url += "/"

        response = self._safe_request(api_url)
        if not response:
            return []

        # Lever pages are HTML, parse the job listings
        soup = BeautifulSoup(response.text, "lxml")
        postings = soup.find_all("div", class_="posting")

        relevant_jobs = []
        for posting in postings:
            title_elem = posting.find("h5") or posting.find("a", class_="posting-title")
            if not title_elem:
                continue

            title = title_elem.get_text(strip=True)
            title_lower = title.lower()

            # Check relevance
            if not any(kw in title_lower for kw in ["data", "analyst", "scientist", "quantitative", "analytics"]):
                continue

            # Get URL
            link = posting.find("a", href=True)
            url = link["href"] if link else ""
            if not url:
                continue

            # Get location
            location_elem = posting.find("span", class_="sort-by-location") or posting.find("span", class_="location")
            location = location_elem.get_text(strip=True) if location_elem else company.get("location", "")

            # Check Bay Area
            bay_area_keywords = ["san francisco", "oakland", "berkeley", "san jose", "bay area",
                                 "mountain view", "remote"]
            location_lower = location.lower()
            if not any(kw in location_lower for kw in bay_area_keywords):
                continue

            relevant_jobs.append(RawJob(
                title=title,
                company=company["name"],
                location=location,
                url=url,
                source=f"lever:{company['name'].lower().replace(' ', '_')}",
                description="",  # Would need to follow link for full description
                is_remote="remote" in location_lower or "remote" in title_lower,
            ))

        return relevant_jobs
