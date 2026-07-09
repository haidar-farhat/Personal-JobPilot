"""Record an externally-made application (extension button / dashboard quick-add).

POST /api/applied/record  {url, title?, company?, lead_source?, source}
  - match existing Job by exact URL
  - else create a minimal Job+Application directly (no scoring — the JD
    isn't available; the watchdog's ranking cycle skips description-less jobs)
  - mark APPLIED via record_status_change; idempotent — never regresses a
    status at/beyond APPLIED (applied/response/interview/rejected/...).
"""

import logging
from urllib.parse import urlparse

from fastapi import APIRouter
from pydantic import BaseModel

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
    url: str
    title: str = ""
    company: str = ""
    lead_source: str = ""
    source: str = "dashboard"   # "extension" | "dashboard"


def _fallback_company(url: str) -> str:
    return urlparse(url).hostname or "unknown"


@router.post("/record")
def record_applied(payload: AppliedPayload):
    session = get_session()
    try:
        job = session.query(Job).filter(Job.url == payload.url).first()
        created = False
        if job is None:
            company = payload.company.strip() or _fallback_company(payload.url)
            job = Job(
                title=payload.title.strip() or "(untitled — edit me)",
                company=company,
                url=payload.url,
                source="applied_manual",
                dedup_hash=payload.url,   # URL-keyed; scanner uses its own hash scheme
            )
            match = session.query(Company).filter(
                Company.name_normalized == normalize_company_name(company)).first()
            if match:
                job.company_id = match.id
            session.add(job)
            session.flush()
            created = True

        app = session.query(Application).filter_by(job_id=job.id).first()
        if app is None:
            app = Application(job_id=job.id, status=ApplicationStatus.FOUND)
            session.add(app)
            session.flush()

        if app.status in _AT_OR_PAST_APPLIED:
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
