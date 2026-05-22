"""Base scanner interface for all job board scrapers."""

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
