"""Companies & advisor tracking (spec 2026-07-08).

New TABLES (companies, application_events) come from Base.metadata.create_all
at engine init. This migration:
  1. ALTERs existing tables:  applications.lead_source, applications.next_action,
     jobs.company_id
  2. Backfills approximate ApplicationEvents from existing date fields
     (source="backfill") — only for applications with zero events.
  3. Links jobs.company_id by normalized company name.

Idempotent. Run from the project root:
    python -m db.migrations.006_add_companies_and_events
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.database import get_engine  # noqa: E402
from db.models import Application, ApplicationEvent, Company, Job  # noqa: E402
from utils.company_names import normalize_company_name  # noqa: E402


ALTERS = (
    ("applications", "lead_source", "VARCHAR(100)"),
    ("applications", "next_action", "TEXT"),
    ("jobs", "company_id", "INTEGER"),
)


def get_session_factory():
    return sessionmaker(bind=get_engine())


def _naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo so mixed naive/aware stamps can be sorted together.

    Legacy DB datetimes round-trip through SQLite as naive values, but
    synthesized "current status" stamps are built fresh with
    datetime.now(timezone.utc). Normalizing everything to naive UTC before
    sorting avoids a naive-vs-aware TypeError.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _add_columns(engine) -> list[str]:
    inspector = inspect(engine)
    added = []
    with engine.begin() as conn:
        for table, col, coltype in ALTERS:
            existing = {c["name"] for c in inspector.get_columns(table)}
            if col not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                added.append(f"{table}.{col}")
    return added


def _backfill_events(session) -> int:
    """Synthesize approximate events from legacy date fields (once)."""
    count = 0
    apps = session.query(Application).all()
    with_events = {
        row[0] for row in session.query(ApplicationEvent.application_id).distinct()
    }
    for app in apps:
        if app.id in with_events:
            continue
        stamps = []
        if app.date_applied:
            stamps.append((_naive_utc(app.date_applied), "applied"))
        if app.response_date:
            stamps.append((_naive_utc(app.response_date), "response_received"))
        if app.interview_date:
            stamps.append((_naive_utc(app.interview_date), "interview"))
        current = app.status.value if app.status else "found"
        if current not in {s for _, s in stamps}:
            stamps.append((_naive_utc(datetime.now(timezone.utc)), current))
        prev = None
        for occurred_at, to_status in sorted(stamps, key=lambda t: t[0]):
            session.add(ApplicationEvent(
                application_id=app.id, occurred_at=occurred_at,
                from_status=prev, to_status=to_status, source="backfill"))
            prev = to_status
            count += 1
    return count


def _link_jobs(session) -> int:
    by_norm = {c.name_normalized: c.id for c in session.query(Company).all()}
    linked = 0
    for job in session.query(Job).filter(Job.company_id.is_(None)).all():
        cid = by_norm.get(normalize_company_name(job.company))
        if cid:
            job.company_id = cid
            linked += 1
    return linked


def run() -> dict:
    engine = get_engine()   # create_all makes the new tables
    added = _add_columns(engine)
    session = get_session_factory()()
    try:
        events = _backfill_events(session)
        linked = _link_jobs(session)
        session.commit()
    finally:
        session.close()
    return {"columns_added": added, "events_backfilled": events, "jobs_linked": linked}


if __name__ == "__main__":
    print(run())
