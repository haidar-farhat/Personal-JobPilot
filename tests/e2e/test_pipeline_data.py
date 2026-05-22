"""Read-only data integrity checks against the live SQLite DB."""

import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def session(repo_root):
    """Open a SQLAlchemy session against the project DB."""
    sys.path.insert(0, str(repo_root))
    from db.database import get_session  # type: ignore
    s = get_session()
    yield s
    s.close()


def test_every_application_has_valid_job(session):
    from db.models import Application, Job  # type: ignore
    orphans = (
        session.query(Application)
        .outerjoin(Job, Application.job_id == Job.id)
        .filter(Job.id.is_(None))
        .count()
    )
    assert orphans == 0, f"{orphans} applications point to deleted jobs"


def test_every_score_has_valid_job(session):
    from db.models import JobScore, Job  # type: ignore
    orphans = (
        session.query(JobScore)
        .outerjoin(Job, JobScore.job_id == Job.id)
        .filter(Job.id.is_(None))
        .count()
    )
    assert orphans == 0


def test_fit_scores_in_valid_range(session):
    from db.models import JobScore  # type: ignore
    bad = session.query(JobScore).filter(
        (JobScore.fit_score < 0) | (JobScore.fit_score > 100)
    ).count()
    assert bad == 0, f"{bad} JobScore rows have out-of-range fit_score"


def test_dedup_hashes_are_unique(session):
    from sqlalchemy import func
    from db.models import Job  # type: ignore
    duplicate_hashes = (
        session.query(Job.dedup_hash, func.count(Job.id))
        .group_by(Job.dedup_hash)
        .having(func.count(Job.id) > 1)
        .all()
    )
    assert not duplicate_hashes, f"duplicate dedup_hash rows: {duplicate_hashes[:5]}"


def test_application_statuses_are_canonical(session):
    from db.models import Application, ApplicationStatus  # type: ignore
    valid = {s.value for s in ApplicationStatus}
    bad = []
    for app in session.query(Application).all():
        s = app.status.value if hasattr(app.status, "value") else str(app.status)
        if s not in valid:
            bad.append((app.id, s))
    assert not bad, f"applications with non-canonical status: {bad[:5]}"
