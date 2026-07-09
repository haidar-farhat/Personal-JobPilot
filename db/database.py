"""Database connection and session management for JobPilot."""

import os
from pathlib import Path

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from db.models import Base


_engine = None
_SessionLocal = None


def _get_db_path() -> str:
    config_path = Path(__file__).parent.parent / "config" / "settings.yaml"
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
        db_name = config.get("output", {}).get("database_path", "jobpilot.db")
    else:
        db_name = "jobpilot.db"
    return str(Path(__file__).parent.parent / db_name)


def get_engine():
    global _engine
    if _engine is None:
        db_path = _get_db_path()
        _engine = create_engine(f"sqlite:///{db_path}", echo=False)
        Base.metadata.create_all(_engine)
    return _engine


def get_session() -> Session:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine())
    return _SessionLocal()


def init_db():
    """Initialize the database, creating all tables."""
    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def record_status_change(session, application, new_status, *, source, note=None):
    """THE single write-path for Application.status.

    Sets the status, stamps the matching date field (preserving the logic
    previously inlined in dashboard.py / review_app.py / runner.py), and
    appends an ApplicationEvent. Same-status calls are no-ops so callers
    can be idempotent for free. Caller commits.
    """
    from datetime import datetime, timezone

    from db.models import ApplicationEvent, ApplicationStatus

    if application.status == new_status:
        return
    old = application.status.value if application.status else None
    application.status = new_status
    now = datetime.now(timezone.utc)
    if new_status == ApplicationStatus.APPLIED and not application.date_applied:
        application.date_applied = now
    elif new_status == ApplicationStatus.RESPONSE_RECEIVED and not application.response_date:
        application.response_date = now
    elif new_status == ApplicationStatus.INTERVIEW and not application.interview_date:
        application.interview_date = now
    session.add(ApplicationEvent(
        application_id=application.id, occurred_at=now,
        from_status=old, to_status=new_status.value,
        note=note, source=source))
