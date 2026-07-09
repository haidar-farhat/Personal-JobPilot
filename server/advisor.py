"""Advisor report — everything that happened since a given date.

Built for Vantage Point meetings: headline stats + per-company application
timelines, aggregated from application_events (the historized record).
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from db.database import get_session
from db.models import Application, ApplicationEvent, Company, Job
from utils.company_names import normalize_company_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_RESPONSES = {"response_received"}
_INTERVIEWS = {"interview"}
# skipped is deliberately excluded — a never-pursued job isn't a "closed
# application" in an advisor conversation (intentional divergence from
# companies.py's closed STAGE, which does include it).
_CLOSED = {"rejected", "no_response", "no_longer_available"}
# An application only earns a spot in the companies sections if at least one
# in-window event hits a milestone — otherwise backfilled found/scored/
# materials_ready churn (hundreds of rows, all stamped migration day) would
# drown the report in pipeline noise.
_MILESTONES = {"applied"} | _RESPONSES | _INTERVIEWS | _CLOSED


@router.get("/report")
def advisor_report(since: str):
    try:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")

    session = get_session()
    try:
        rows = (session.query(ApplicationEvent, Application, Job, Company)
                .join(Application, ApplicationEvent.application_id == Application.id)
                .join(Job, Application.job_id == Job.id)
                .outerjoin(Company, Job.company_id == Company.id)
                .filter(ApplicationEvent.occurred_at >= since_dt.replace(tzinfo=None))
                .order_by(ApplicationEvent.occurred_at)
                .all())

        # Distinct applications per bucket — "how many applications did you
        # send" must not double-count an app that re-enters a status.
        buckets: dict[str, set] = {"applied": set(), "responses": set(),
                                   "interviews": set(), "closed": set()}
        by_app: dict[int, dict] = {}
        for ev, app, job, company in rows:
            if ev.to_status == "applied":
                buckets["applied"].add(app.id)
            elif ev.to_status in _RESPONSES:
                buckets["responses"].add(app.id)
            elif ev.to_status in _INTERVIEWS:
                buckets["interviews"].add(app.id)
            elif ev.to_status in _CLOSED:
                buckets["closed"].add(app.id)
            entry = by_app.setdefault(app.id, {
                # canonical grouping: company_id when linked, normalized name
                # otherwise — raw display strings would split "Brex" from
                # "Brex, Inc." (mirrors server/companies.py's rollup rule)
                "_group": (job.company_id if job.company_id is not None
                           else f"name:{normalize_company_name(job.company)}"),
                "_display": company.name if company else job.company,
                "_milestone": False,
                "title": job.title, "company": job.company,
                "company_id": job.company_id, "url": job.url,
                "status": app.status.value, "lead_source": app.lead_source,
                "next_action": app.next_action,
                "date_applied": app.date_applied.isoformat() if app.date_applied else None,
                "events": []})
            if ev.to_status in _MILESTONES:
                entry["_milestone"] = True
            entry["events"].append({
                "occurred_at": ev.occurred_at.isoformat(),
                "from": ev.from_status, "to": ev.to_status,
                "source": ev.source, "approx": ev.source == "backfill"})

        stats = {name: len(ids) for name, ids in buckets.items()}

        by_group: dict = defaultdict(list)
        display: dict = {}
        for entry in by_app.values():
            milestone = entry.pop("_milestone")
            group = entry.pop("_group")
            name = entry.pop("_display")
            if not milestone:
                continue   # in-window events, but all pipeline churn
            display.setdefault(group, name)
            by_group[group].append(entry)
        companies = [{"company": display[g], "applications": by_group[g]}
                     for g in sorted(by_group, key=lambda g: display[g])]
        return {"since": since, "generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": stats, "companies": companies}
    finally:
        session.close()
