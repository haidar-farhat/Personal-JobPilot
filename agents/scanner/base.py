"""Base scanner interface for all job board scrapers."""

import re
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import requests

from db.database import get_session
from db.models import Job, Application, ApplicationStatus, ScanLog
from utils.dedup import generate_dedup_hash, is_duplicate, merge_duplicate
from utils.comp import parse_hourly, parse_employment_type

logger = logging.getLogger(__name__)


@dataclass
class RawJob:
    """Raw job data from a scanner before DB insertion."""
    title: str
    company: str
    location: str
    url: str
    source: str
    description: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_text: str = ""
    source_id: str = ""
    date_posted: datetime | None = None
    is_remote: bool = False
    seniority_level: str = ""


class BaseScanner(ABC):
    """Abstract base class for job board scanners."""

    source_name: str = "unknown"
    rate_limit_seconds: float = 2.0  # Seconds between requests

    def __init__(self, config: dict):
        self.config = config
        self.search_config = config.get("search", {})
        self.keywords = self.search_config.get("keywords", [])
        self.locations = self.search_config.get("locations", [])
        self.excluded_keywords = self.search_config.get("excluded_title_keywords", [])
        self.excluded_locations = self.search_config.get("excluded_locations", [])
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })

    @abstractmethod
    def scan(self) -> list[RawJob]:
        """Scan for new jobs. Must be implemented by subclasses."""
        pass

    def _should_skip_title(self, title: str) -> bool:
        """Check if a job title should be skipped based on excluded keywords."""
        title_lower = title.lower()
        for keyword in self.excluded_keywords:
            if keyword.lower() in title_lower:
                return True
        return False

    # US-eligibility signals. A posting that names any of these is keepable even
    # if it ALSO names a foreign office — big ATS boards routinely list one req
    # as "San Francisco, CA | London, UK", and a naive deny-list would throw the
    # US-eligible half away.
    _US_SIGNALS = (
        "united states", "usa", "u.s.", " us;", " us)", ", us", "remote - us",
        "us remote", "nationwide", "anywhere in the us",
    )
    _US_STATE_CODES = (
        "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
        "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
        "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
        "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
        "WI", "WY", "DC",
    )
    # ", CA" only when the code ends a segment: end-of-string or a separator.
    _US_STATE_RE = re.compile(
        r",\s*(?:" + "|".join(_US_STATE_CODES) + r")\s*(?:$|[;|/)\],\n])"
    )
    # "Toronto, ON, CA" — here CA is the ISO code for Canada, not California, so
    # the US-state escape above must not fire. US postings write ", CA" after a
    # city ("San Francisco, CA"), never after another region code.
    _CA_IS_CANADA_RE = re.compile(
        r",\s*(?:ON|BC|AB|QC|NS|MB|SK|NB|PE|NL|YT|NT|NU)\s*,\s*CA\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _deny_pattern(terms) -> "re.Pattern | None":
        """Compile the deny-list with word boundaries.

        Plain substring matching silently eats US locations that merely contain
        a country name: "india" is inside "Indiana"/"Indianapolis", "oman" is
        inside "Romansville". Boundaries are only added on ends that are word
        characters, so entries like ", uk" and "uk&i" still match correctly.
        """
        parts = []
        for t in terms:
            t = (t or "").strip().lower()
            if not t:
                continue
            p = re.escape(t)
            if t[0].isalnum():
                p = r"\b" + p
            if t[-1].isalnum():
                p = p + r"\b"
            parts.append(p)
        return re.compile("|".join(parts)) if parts else None

    def _should_skip_location(self, location: str) -> bool:
        """True if the posting is for a location Matthew can't take.

        Deny-list from settings.yaml `search.excluded_locations`, overridden by
        any US-eligibility signal in the same string. Blank locations pass —
        plenty of real US reqs omit it, and the ranker scores those properly.

        This is deliberately a coarse non-US gate, NOT a target-metro allow-list:
        location strings are too messy ("US, CA, Santa Clara", "Remote-Friendly,
        United States") for exact matching, and metro preference is nuanced work
        the LLM ranker already does. Point of this gate is only to stop paying
        for LLM calls on reqs in Bangalore or Tokyo.
        """
        if not self.excluded_locations:
            return False
        loc = (location or "").strip().lower()
        if not loc:
            return False

        pat = getattr(self, "_deny_pat_cache", None)
        if pat is None:
            pat = self._deny_pattern(self.excluded_locations)
            self._deny_pat_cache = pat
        if pat is None or not pat.search(loc):
            return False

        # Named a foreign location — keep it anyway if it's also US-eligible.
        if self._CA_IS_CANADA_RE.search(location or ""):
            return True
        if any(sig in loc for sig in self._US_SIGNALS):
            return False
        # ", CA" / ", TX" style state suffixes. Matched UPPERCASE against the
        # original string and anchored to a segment end, because the loose
        # version has two false positives that both occur in real postings:
        #   "Canada - Remote (ON, AB, BC, or NS Only)" -> ", or" reads as Oregon
        #   "London, ON" (Ontario) -> not a US state, must stay rejected
        if re.search(self._US_STATE_RE, location or ""):
            return False
        return True

    # Core role terms always treated as relevant (preserves legacy data/analyst
    # recall) — unioned with the configured search keywords. Matched on word
    # boundaries so short tokens (e.g. "rbt") don't hit inside unrelated words and
    # "data" doesn't match "Database".
    _LEGACY_RELEVANCE_TERMS = (
        "data", "analyst", "scientist", "quantitative", "analytics",
        "machine learning", "ml engineer",
    )

    def _relevance_pattern(self) -> "re.Pattern | None":
        """Compile (once) a word-boundary alternation of all relevance terms."""
        pat = getattr(self, "_rel_pat_cache", None)
        if pat is None:
            terms = set(self._LEGACY_RELEVANCE_TERMS)
            for kw in self.keywords:
                t = (kw or "").strip().lower()
                if len(t) >= 3:           # skip 1-2 char noise
                    terms.add(t)
            escaped = sorted((re.escape(t) for t in terms if t), key=len, reverse=True)
            pat = re.compile(r"\b(?:" + "|".join(escaped) + r")\b") if escaped else None
            self._rel_pat_cache = pat
        return pat

    def _title_is_relevant(self, title: str) -> bool:
        """True if the title matches any configured/legacy relevance term.

        Used by ATS scanners as a coarse pre-filter before fetching/parsing
        full job detail. Driven by config (settings.yaml search.keywords) so AI
        and BT roles are picked up, not just data/analyst.
        """
        pat = self._relevance_pattern()
        if pat is None:
            return True
        return bool(pat.search((title or "").lower()))

    def _rate_limit(self):
        """Sleep to respect rate limits."""
        time.sleep(self.rate_limit_seconds)

    def _safe_request(self, url: str, **kwargs) -> requests.Response | None:
        """Make an HTTP request with error handling and rate limiting."""
        try:
            self._rate_limit()
            response = self.session.get(url, timeout=30, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            logger.warning(f"[{self.source_name}] Request failed for {url}: {e}")
            return None

    def run(self) -> dict:
        """Execute a full scan cycle: scrape, dedup, store, log.

        Returns:
            Dict with scan results: {jobs_found, jobs_new, errors}
        """
        start_time = time.time()
        errors = []
        jobs_found = 0
        jobs_new = 0

        try:
            raw_jobs = self.scan()
            jobs_found = len(raw_jobs)
            logger.info(f"[{self.source_name}] Found {jobs_found} raw jobs")

            for raw_job in raw_jobs:
                try:
                    # Skip excluded titles
                    if self._should_skip_title(raw_job.title):
                        logger.debug(f"[{self.source_name}] Skipping excluded title: {raw_job.title}")
                        continue

                    # Skip non-US postings before they cost an LLM scoring call
                    if self._should_skip_location(raw_job.location):
                        logger.debug(
                            f"[{self.source_name}] Skipping non-US location "
                            f"'{raw_job.location}': {raw_job.title}"
                        )
                        continue

                    # Check for duplicates
                    existing = is_duplicate(raw_job.company, raw_job.title, raw_job.location)
                    if existing:
                        merge_duplicate(existing, raw_job.source, raw_job.url)
                        logger.debug(f"[{self.source_name}] Duplicate found: {raw_job.title} at {raw_job.company}")
                        continue

                    # Store new job
                    self._store_job(raw_job)
                    jobs_new += 1

                except Exception as e:
                    error_msg = f"Error processing job '{raw_job.title}': {e}"
                    logger.error(f"[{self.source_name}] {error_msg}")
                    errors.append(error_msg)

        except Exception as e:
            error_msg = f"Scan failed: {e}"
            logger.error(f"[{self.source_name}] {error_msg}")
            errors.append(error_msg)

        duration = time.time() - start_time

        # Log scan results
        self._log_scan(jobs_found, jobs_new, errors, duration)

        result = {
            "source": self.source_name,
            "jobs_found": jobs_found,
            "jobs_new": jobs_new,
            "errors": errors,
            "duration_seconds": round(duration, 2),
        }
        logger.info(f"[{self.source_name}] Scan complete: {jobs_new} new / {jobs_found} total ({duration:.1f}s)")
        return result

    def _store_job(self, raw_job: RawJob):
        """Store a new job in the database."""
        session = get_session()
        try:
            # Parse hourly comp + employment type (BT track). Look in the comp text
            # first, then the start of the description.
            comp_blob = f"{raw_job.salary_text or ''}  {(raw_job.description or '')[:800]}"
            hourly = parse_hourly(comp_blob)
            emp_type = parse_employment_type(raw_job.title, raw_job.description)
            if hourly:
                pay_period = "hourly"
            elif raw_job.salary_min or raw_job.salary_max:
                pay_period = "annual"
            else:
                pay_period = "unknown"

            job = Job(
                title=raw_job.title,
                company=raw_job.company,
                location=raw_job.location,
                salary_min=raw_job.salary_min,
                salary_max=raw_job.salary_max,
                salary_text=raw_job.salary_text,
                description=raw_job.description,
                url=raw_job.url,
                source=raw_job.source,
                source_id=raw_job.source_id,
                date_posted=raw_job.date_posted,
                is_remote=raw_job.is_remote,
                seniority_level=raw_job.seniority_level,
                pay_period=pay_period,
                hourly_min=hourly[0] if hourly else None,
                hourly_max=hourly[1] if hourly else None,
                employment_type=emp_type,
                dedup_hash=generate_dedup_hash(raw_job.company, raw_job.title, raw_job.location),
            )
            session.add(job)

            # Create initial application record
            session.flush()  # Get the job ID
            application = Application(job_id=job.id, status=ApplicationStatus.FOUND)
            session.add(application)

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _log_scan(self, jobs_found: int, jobs_new: int, errors: list, duration: float):
        """Log scan results to database."""
        session = get_session()
        try:
            log = ScanLog(
                source=self.source_name,
                jobs_found=jobs_found,
                jobs_new=jobs_new,
                errors="\n".join(errors) if errors else None,
                duration_seconds=duration,
            )
            session.add(log)
            session.commit()
        except Exception:
            session.rollback()
        finally:
            session.close()
