"""Record an externally-made application (extension button / dashboard quick-add).

POST /api/applied/record  {url, title?, company?, lead_source?, source}
  - match existing Job: (a) exact URL, (b) canonical URL (hostname+path,
    tracking params/fragment stripped), (c) normalized title+company
  - else create a minimal Job+Application directly (no scoring — the JD
    isn't available; the watchdog's ranking cycle skips description-less jobs)
  - mark APPLIED via record_status_change; idempotent — never regresses a
    status at/beyond APPLIED (applied/response/interview/rejected/...).
"""

import hashlib
import logging
from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from db.database import get_session, record_status_change
from db.models import Application, ApplicationStatus, Company, Job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/applied", tags=["applied"])

# Stages at-or-beyond APPLIED — recording again must be a no-op.
_AT_OR_PAST_APPLIED = {
    ApplicationStatus.APPLIED, ApplicationStatus.RESPONSE_RECEIVED,
    ApplicationStatus.INTERVIEW, ApplicationStatus.REJECTED,
    ApplicationStatus.NO_RESPONSE,
}


class AppliedPayload(BaseModel):
    url: str = Field(min_length=1)
    title: str = ""
    company: str = ""
    lead_source: str = ""
    source: Literal["extension", "dashboard"] = "dashboard"


def _fallback_company(url: str) -> str:
    return urlparse(url).hostname or "unknown"


def _dedup_hash(url: str) -> str:
    # sha256 hex = 64 chars — fits Job.dedup_hash String(64) exactly;
    # still URL-keyed and unique (raw URLs overflow the column).
    return hashlib.sha256(url.encode()).hexdigest()


def _canonical(u: str) -> str:
    """Lowercased hostname + path, no query/fragment, no trailing slash."""
    p = urlparse(u)
    return f"{(p.hostname or '').lower()}{p.path}".rstrip("/")


def _match_by_url(session, url: str) -> Job | None:
    """(a) exact URL; (b) canonical URL — real ATS links carry tracking
    params (?gh_src=, utm_*) the scanner-stored URL doesn't have."""
    job = session.query(Job).filter(Job.url == url).first()
    if job is not None:
        return job
    path = urlparse(url).path.rstrip("/")
    if len(path) > 5:   # skip trivial paths like "/" or "/jobs" — too many false hits
        canon = _canonical(url)
        for cand in session.query(Job).filter(Job.url.contains(path)):
            if _canonical(cand.url) == canon:
                return cand
    return None


def _match_by_title_company(session, title: str, company: str) -> Job | None:
    """(c) normalized-company + casefolded-title equality, python-side
    (normalize_company_name has no SQL equivalent)."""
    title = title.strip()
    company = company.strip()
    if not title or not company:
        return None
    want_company = normalize_company_name(company)
    want_title = title.casefold()
    for cand in session.query(Job):
        if (normalize_company_name(cand.company) == want_company
                and (cand.title or "").strip().casefold() == want_title):
            return cand
    return None


def _find_job(session, payload: AppliedPayload) -> Job | None:
    job = _match_by_url(session, payload.url)
    if job is None:
        job = _match_by_title_company(session, payload.title, payload.company)
    return job


@router.post("/record")
def record_applied(payload: AppliedPayload):
    session = get_session()
    try:
        job = _find_job(session, payload)
        created = False
        if job is None:
            company = payload.company.strip() or _fallback_company(payload.url)
            new_job = Job(
                title=payload.title.strip() or "(untitled — edit me)",
                company=company,
                url=payload.url,
                source="applied_manual",
                dedup_hash=_dedup_hash(payload.url),
            )
            match = session.query(Company).filter(
                Company.name_normalized == normalize_company_name(company)).first()
            if match:
                new_job.company_id = match.id
            try:
                session.add(new_job)
                session.flush()
                job = new_job
                created = True
            except IntegrityError:
                # Double-click race: a concurrent request inserted this job
                # between our match and flush. Recover gracefully.
                session.rollback()
                job = _match_by_url(session, payload.url)
                if job is None:   # UNIQUE(dedup_hash) fired but URL differs
                    job = session.query(Job).filter(
                        Job.dedup_hash == _dedup_hash(payload.url)).first()
                if job is None:
                    raise       # not the race we know how to recover from

        app = session.query(Application).filter_by(job_id=job.id).first()
        if app is None:
            app = Application(job_id=job.id, status=ApplicationStatus.FOUND)
            session.add(app)
            session.flush()

        if app.status in _AT_OR_PAST_APPLIED:
            # Only writer of lead_source in the codebase — backfill rows
            # (e.g. bot-applied) that never got one.
            if payload.lead_source and not app.lead_source:
                app.lead_source = payload.lead_source
            session.commit()
            return {"ok": True, "already": True, "created": created,
                    "matched": not created, "job_id": job.id,
                    "status": app.status.value}

        record_status_change(session, app, ApplicationStatus.APPLIED,
                             source=payload.source)
        app.lead_source = payload.lead_source or payload.source
        session.commit()
        return {"ok": True, "already": False, "created": created,
                "matched": not created, "job_id": job.id, "status": "applied"}
    finally:
        session.close()
