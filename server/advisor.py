"""Advisor report — everything that happened since a given date.

Built for Vantage Point meetings: headline stats + per-company application
timelines, aggregated from application_events (the historized record).
"""

import logging
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from db.database import get_session
from db.models import Application, ApplicationEvent, Job

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/advisor", tags=["advisor"])

_RESPONSES = {"response_received"}
_INTERVIEWS = {"interview"}
_CLOSED = {"rejected", "no_response", "no_longer_available"}


@router.get("/report")
def advisor_report(since: str):
    try:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="since must be YYYY-MM-DD")

    session = get_session()
    try:
        rows = (session.query(ApplicationEvent, Application, Job)
                .join(Application, ApplicationEvent.application_id == Application.id)
                .join(Job, Application.job_id == Job.id)
                .filter(ApplicationEvent.occurred_at >= since_dt.replace(tzinfo=None))
                .order_by(ApplicationEvent.occurred_at)
                .all())

        stats = {"applied": 0, "responses": 0, "interviews": 0, "closed": 0}
        by_app: dict[int, dict] = {}
        for ev, app, job in rows:
            if ev.to_status == "applied":
                stats["applied"] += 1
            elif ev.to_status in _RESPONSES:
                stats["responses"] += 1
            elif ev.to_status in _INTERVIEWS:
                stats["interviews"] += 1
            elif ev.to_status in _CLOSED:
                stats["closed"] += 1
            entry = by_app.setdefault(app.id, {
                "title": job.title, "company": job.company,
                "company_id": job.company_id, "url": job.url,
                "status": app.status.value, "lead_source": app.lead_source,
                "next_action": app.next_action,
                "date_applied": app.date_applied.isoformat() if app.date_applied else None,
                "events": []})
            entry["events"].append({
                "occurred_at": ev.occurred_at.isoformat(),
                "from": ev.from_status, "to": ev.to_status,
                "source": ev.source, "approx": ev.source == "backfill"})

        by_company: dict[str, list] = defaultdict(list)
        for entry in by_app.values():
            by_company[entry["company"]].append(entry)
        companies = [{"company": name, "applications": apps}
                     for name, apps in sorted(by_company.items())]
        return {"since": since, "generated_at": datetime.now(timezone.utc).isoformat(),
                "stats": stats, "companies": companies}
    finally:
        session.close()
