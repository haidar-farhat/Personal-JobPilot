"""Companies API — tailored profiles + per-company pipeline trees."""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlalchemy import case

from db.database import get_session
from db.models import Application, ApplicationStatus, Company, Job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["companies"])

# Company.priority is a STRING — a plain .desc() sorts alphabetically
# (medium > low > high), burying high-priority companies. Rank explicitly.
_PRIORITY_RANK = case((Company.priority == "high", 0),
                      (Company.priority == "medium", 1),
                      (Company.priority == "low", 2),
                      else_=3)

# Pipeline stage groups (spec §4).
STAGE_GROUPS = {
    "watching": {ApplicationStatus.FOUND, ApplicationStatus.SCORED,
                 ApplicationStatus.MATERIALS_READY, ApplicationStatus.QUEUED,
                 ApplicationStatus.APPROVED},
    "applied": {ApplicationStatus.APPLIED},
    "in_play": {ApplicationStatus.RESPONSE_RECEIVED, ApplicationStatus.INTERVIEW},
    "closed": {ApplicationStatus.REJECTED, ApplicationStatus.NO_RESPONSE,
               ApplicationStatus.NO_LONGER_AVAILABLE, ApplicationStatus.SKIPPED},
}
STAGE_ORDER = ("watching", "applied", "in_play", "closed")


def _stage_of(status: ApplicationStatus) -> str:
    for stage, members in STAGE_GROUPS.items():
        if status in members:
            return stage
    return "watching"


def _lazy_link(session) -> None:
    """Link any unlinked jobs to companies by normalized name.

    Runs at the top of both GET endpoints so jobs found by scanners AFTER
    seeding attach to their company without touching scanner code. Cheap:
    only scans company_id IS NULL rows (a few hundred max, local SQLite).
    """
    by_norm = {}
    for c in session.query(Company).order_by(Company.id).all():
        by_norm.setdefault(c.name_normalized, c.id)
    dirty = False
    for job in session.query(Job).filter(Job.company_id.is_(None)).all():
        cid = by_norm.get(normalize_company_name(job.company))
        if cid:
            job.company_id = cid
            dirty = True
    if dirty:
        session.commit()


def _teaser(md: str | None) -> str:
    if not md:
        return ""
    return md.strip().splitlines()[0][:200]


def _company_dict(c: Company) -> dict:
    return {
        "id": c.id, "name": c.name, "careers_url": c.careers_url,
        "ats_platform": c.ats_platform, "priority": c.priority,
        "status": c.status, "suggested": c.suggested,
        "profile_source": c.profile_source, "draft_status": c.draft_status,
        "last_refreshed_at": c.last_refreshed_at.isoformat() if c.last_refreshed_at else None,
    }


@router.get("/companies")
def list_companies():
    session = get_session()
    try:
        _lazy_link(session)
        rows = (session.query(Job, Application)
                .outerjoin(Application, Application.job_id == Job.id)
                .all())
        _zero = lambda: {"watching": 0, "applied": 0, "in_play": 0, "closed": 0}
        counts: dict[int, dict] = defaultdict(_zero)
        last_activity: dict[int | None, datetime] = {}
        # Unlinked jobs rolled up by normalized name so nothing is invisible.
        other_counts: dict[str, dict] = defaultdict(_zero)
        other_names: dict[str, str] = {}
        for job, app in rows:
            status = app.status if app else ApplicationStatus.FOUND
            stage = _stage_of(status)
            if job.company_id is None:
                norm = normalize_company_name(job.company)
                other_names.setdefault(norm, job.company)
                other_counts[norm][stage] += 1
            else:
                counts[job.company_id][stage] += 1
            stamp = (app.date_applied or job.date_found) if app else job.date_found
            if stamp and (job.company_id not in last_activity or stamp > last_activity[job.company_id]):
                last_activity[job.company_id] = stamp

        companies = []
        for c in session.query(Company).order_by(_PRIORITY_RANK, Company.name).all():
            d = _company_dict(c)
            d["counts"] = counts.get(c.id, _zero())
            la = last_activity.get(c.id)
            d["last_activity"] = la.isoformat() if la else None
            d["why_fit_teaser"] = _teaser(c.why_fit_md)
            companies.append(d)

        other = [{"name": other_names[norm], "counts": other_counts[norm]}
                 for norm in sorted(other_counts)]
        return {"companies": companies, "other": other}
    finally:
        session.close()


@router.get("/company/{company_id}")
def company_detail(company_id: int):
    session = get_session()
    try:
        _lazy_link(session)
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        d = _company_dict(c)
        d.update(overview_md=c.overview_md, why_fit_md=c.why_fit_md,
                 hiring_bar_md=c.hiring_bar_md, notes_md=c.notes_md)

        groups = {stage: [] for stage in STAGE_ORDER}
        rows = (session.query(Job, Application)
                .outerjoin(Application, Application.job_id == Job.id)
                .filter(Job.company_id == company_id)
                .order_by(Job.date_found.desc())
                .all())
        for job, app in rows:
            status = app.status if app else ApplicationStatus.FOUND
            entry = {
                "job_id": job.id, "app_id": app.id if app else None,
                "title": job.title, "url": job.url,
                "fit_score": job.score.fit_score if job.score else None,
                "status": status.value,
                "date_found": job.date_found.isoformat() if job.date_found else None,
                "date_applied": (app.date_applied.isoformat()
                                 if app and app.date_applied else None),
                "lead_source": app.lead_source if app else None,
                "next_action": app.next_action if app else None,
                "events": [
                    {"occurred_at": e.occurred_at.isoformat(),
                     "from": e.from_status, "to": e.to_status,
                     "source": e.source, "note": e.note}
                    for e in (app.events if app else [])
                ],
            }
            groups[_stage_of(status)].append(entry)
        d["tree"] = [{"stage": s, "jobs": groups[s]} for s in STAGE_ORDER]
        return d
    finally:
        session.close()


class CompanyCreate(BaseModel):
    name: str
    careers_url: str = ""


class CompanyPatch(BaseModel):
    # all optional — patch semantics
    name: str | None = None
    careers_url: str | None = None
    ats_platform: str | None = None
    priority: str | None = None
    status: str | None = None
    overview_md: str | None = None
    why_fit_md: str | None = None
    hiring_bar_md: str | None = None
    notes_md: str | None = None


_PROFILE_FIELDS = {"overview_md", "why_fit_md", "hiring_bar_md"}


def draft_profile_task(company_id: int) -> None:
    """Phase-3 hook (agents/company_profiler.py). Until the profiler lands,
    clear draft_status so cards don't hang in 'drafting'."""
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if c and c.draft_status == "drafting":
            c.draft_status = None
            session.commit()
    finally:
        session.close()


@router.post("/companies")
def create_company(payload: CompanyCreate, background_tasks: BackgroundTasks):
    norm = normalize_company_name(payload.name)
    if not norm:
        raise HTTPException(status_code=400, detail="Company name required")
    session = get_session()
    try:
        if session.query(Company).filter_by(name_normalized=norm).first():
            raise HTTPException(status_code=409, detail="Company already exists")
        c = Company(name=payload.name.strip(), name_normalized=norm,
                    careers_url=payload.careers_url or None,
                    profile_source="manual", draft_status="drafting")
        session.add(c)
        # adopt any unlinked jobs that match
        session.flush()
        for job in session.query(Job).filter(Job.company_id.is_(None)).all():
            if normalize_company_name(job.company) == norm:
                job.company_id = c.id
        session.commit()
        background_tasks.add_task(draft_profile_task, c.id)
        return {"id": c.id, "draft_status": "drafting"}
    finally:
        session.close()


@router.patch("/company/{company_id}")
def patch_company(company_id: int, payload: CompanyPatch):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        data = payload.model_dump(exclude_unset=True)
        for field, value in data.items():
            setattr(c, field, value)
        if data.keys() & _PROFILE_FIELDS:
            c.profile_source = "manual"
        if "name" in data:
            c.name_normalized = normalize_company_name(data["name"])
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.delete("/company/{company_id}")
def delete_company(company_id: int):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        for job in session.query(Job).filter(Job.company_id == company_id).all():
            job.company_id = None
        session.delete(c)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


@router.post("/company/{company_id}/refresh")
def refresh_company(company_id: int, background_tasks: BackgroundTasks):
    session = get_session()
    try:
        c = session.query(Company).get(company_id)
        if not c:
            raise HTTPException(status_code=404, detail="Company not found")
        c.profile_backup = {"overview_md": c.overview_md,
                            "why_fit_md": c.why_fit_md,
                            "hiring_bar_md": c.hiring_bar_md,
                            "profile_source": c.profile_source}
        c.draft_status = "drafting"
        session.commit()
        background_tasks.add_task(draft_profile_task, company_id)
        return {"ok": True, "draft_status": "drafting"}
    finally:
        session.close()
