"""Companies & advisor tracking (spec 2026-07-08).

New TABLES (companies, application_events) come from Base.metadata.create_all
at engine init. This migration:
  1. ALTERs existing tables:  applications.lead_source, applications.next_action,
     jobs.company_id
  2. Creates idx_jobs_company_id (create_all skips indexes on pre-existing
     tables, so the model-declared index must be created here).
  3. Backfills approximate ApplicationEvents from existing date fields
     (source="backfill") — only for applications with zero events.
  4. Links jobs.company_id by normalized company name.

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
    sorting avoids a naive-vs-aware TypeError. Naive inputs are assumed to
    already be UTC (this codebase writes UTC datetimes) and pass through
    unchanged.
    """
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _add_columns(engine) -> list[str]:
    # One fresh column snapshot per table, read before the loop. (The
    # Inspector caches get_columns per table anyway, so an in-loop re-read
    # would see the cache, not fresh state; cross-run idempotency comes from
    # building a new inspector each run().)
    inspector = inspect(engine)
    existing = {table: {c["name"] for c in inspector.get_columns(table)}
                for table in {t for t, _, _ in ALTERS}}
    added = []
    with engine.begin() as conn:
        for table, col, coltype in ALTERS:
            if col not in existing[table]:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}"))
                added.append(f"{table}.{col}")
    return added


def _add_indexes(engine) -> list[str]:
    # Base.metadata.create_all only creates indexes for tables it creates —
    # the pre-existing jobs table got the company_id COLUMN from _add_columns
    # but not the model-declared idx_jobs_company_id, so create it here.
    added = []
    with engine.begin() as conn:
        exists = conn.execute(text(
            "SELECT 1 FROM sqlite_master WHERE type='index' "
            "AND name='idx_jobs_company_id'")).fetchone()
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_jobs_company_id ON jobs(company_id)"))
        if exists is None:
            added.append("idx_jobs_company_id")
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
        # Chains are time-sorted only, not status-order-validated: dirty
        # legacy dates yield time-true but logically-backward chains —
        # accepted for an approximate backfill.
        for occurred_at, to_status in sorted(stamps, key=lambda t: t[0]):
            session.add(ApplicationEvent(
                application_id=app.id, occurred_at=occurred_at,
                from_status=prev, to_status=to_status, source="backfill"))
            prev = to_status
            count += 1
    return count


def _link_jobs(session) -> int:
    # If two companies normalize to the same key, the first-created (lowest
    # id) deterministically wins.
    by_norm: dict[str, int] = {}
    for c in session.query(Company).order_by(Company.id).all():
        by_norm.setdefault(c.name_normalized, c.id)
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
    indexes = _add_indexes(engine)
    session = get_session_factory()()
    try:
        events = _backfill_events(session)
        linked = _link_jobs(session)
        session.commit()
    finally:
        session.close()
    return {"columns_added": added, "indexes_added": indexes,
            "events_backfilled": events, "jobs_linked": linked}


if __name__ == "__main__":
    print(run())
