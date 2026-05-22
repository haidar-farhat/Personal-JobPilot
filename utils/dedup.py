"""Job deduplication utilities for JobPilot."""

import hashlib
import re

from thefuzz import fuzz

from db.database import get_session
from db.models import Job


def normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip whitespace, remove special chars."""
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text


def extract_city(location: str) -> str:
    """Extract city name from a location string."""
    if not location:
        return ""
    # Handle "City, State" format
    parts = location.split(",")
    return normalize_text(parts[0])


def generate_dedup_hash(company: str, title: str, location: str) -> str:
    """Generate a deduplication hash from job key fields."""
    normalized = f"{normalize_text(company)}|{normalize_text(title)}|{extract_city(location)}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def is_duplicate(company: str, title: str, location: str, fuzzy_threshold: int = 85) -> Job | None:
    """Check if a job already exists in the database.

    First checks exact hash match, then fuzzy matches on company+title.

    Returns:
        The existing Job if duplicate found, None otherwise.
    """
    dedup_hash = generate_dedup_hash(company, title, location)
    session = get_session()

    try:
        # Exact hash match
        existing = session.query(Job).filter_by(dedup_hash=dedup_hash).first()
        if existing:
            return existing

        # Fuzzy matching on recent jobs (last 30 days) from same approximate company
        norm_company = normalize_text(company)
        norm_title = normalize_text(title)

        recent_jobs = session.query(Job).filter(
            Job.company.ilike(f"%{company[:10]}%")
        ).limit(100).all()

        for job in recent_jobs:
            company_score = fuzz.ratio(norm_company, normalize_text(job.company))
            title_score = fuzz.ratio(norm_title, normalize_text(job.title))

            # Both company and title must be similar
            if company_score >= fuzzy_threshold and title_score >= fuzzy_threshold:
                return job

        return None
    finally:
        session.close()


def merge_duplicate(existing_job: Job, new_source: str, new_url: str):
    """Add a new source URL to an existing duplicate job."""
    session = get_session()
    try:
        job = session.query(Job).get(existing_job.id)
        if job:
            extra_urls = job.extra_urls or []
            source_entry = {"source": new_source, "url": new_url}
            if source_entry not in extra_urls:
                extra_urls.append(source_entry)
                job.extra_urls = extra_urls
                session.commit()
    finally:
        session.close()
