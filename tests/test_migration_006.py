"""Migration 006 — idempotent column adds + event/company_id backfill."""

import importlib
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from db.models import (
    Application, ApplicationEvent, ApplicationStatus, Company, Job,
)

mig = importlib.import_module("db.migrations.006_add_companies_and_events")


def _seed_legacy_app(session, status, date_applied=None, response_date=None):
    job = Job(title="T", company="Chime Financial, Inc",
              url=f"https://x.test/{status.value}", source="test",
              dedup_hash=f"h-{status.value}")
    session.add(job)
    session.flush()
    app = Application(job_id=job.id, status=status,
                      date_applied=date_applied, response_date=response_date)
    session.add(app)
    session.commit()
    return app


def test_run_is_idempotent_and_backfills(tmp_engine, tmp_session, monkeypatch):
    monkeypatch.setattr(mig, "get_engine", lambda: tmp_engine)
    monkeypatch.setattr(mig, "get_session_factory", lambda: (lambda: tmp_session))

    applied_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    _seed_legacy_app(tmp_session, ApplicationStatus.APPLIED, date_applied=applied_at)
    tmp_session.add(Company(name="Chime", name_normalized="chime"))
    tmp_session.commit()

    # create_all already made the model-declared index in tmp; drop it so
    # run() has to recreate it (as it must against the real pre-006 DB,
    # where create_all skipped indexes on the pre-existing jobs table).
    with tmp_engine.begin() as conn:
        conn.execute(text("DROP INDEX idx_jobs_company_id"))

    r1 = mig.run()
    assert r1["events_backfilled"] >= 1
    assert r1["jobs_linked"] == 1
    assert r1["indexes_added"] == ["idx_jobs_company_id"]
    idx_names = {ix["name"] for ix in inspect(tmp_engine).get_indexes("jobs")}
    assert "idx_jobs_company_id" in idx_names

    ev = tmp_session.query(ApplicationEvent).one()
    assert ev.from_status is None
    assert ev.to_status == "applied"
    assert ev.source == "backfill"
    assert ev.occurred_at.replace(tzinfo=timezone.utc) == applied_at

    job = tmp_session.query(Job).one()
    company = tmp_session.query(Company).one()
    assert job.company_id == company.id

    # Second run: nothing double-backfilled, index already present
    r2 = mig.run()
    assert r2["events_backfilled"] == 0
    assert r2["indexes_added"] == []
    assert tmp_session.query(ApplicationEvent).count() == 1


def test_backfill_chains_multi_stamp_history(tmp_engine, tmp_session, monkeypatch):
    monkeypatch.setattr(mig, "get_engine", lambda: tmp_engine)
    monkeypatch.setattr(mig, "get_session_factory", lambda: (lambda: tmp_session))

    job = Job(title="T", company="Plaid", url="https://x.test/multi",
              source="test", dedup_hash="h-multi")
    tmp_session.add(job)
    tmp_session.flush()
    app = Application(
        job_id=job.id, status=ApplicationStatus.REJECTED,
        date_applied=datetime(2026, 6, 1, tzinfo=timezone.utc),
        response_date=datetime(2026, 6, 10, tzinfo=timezone.utc),
        interview_date=datetime(2026, 6, 20, tzinfo=timezone.utc))
    tmp_session.add(app)
    tmp_session.commit()

    mig.run()

    evs = (tmp_session.query(ApplicationEvent)
           .order_by(ApplicationEvent.occurred_at).all())
    assert [(e.from_status, e.to_status) for e in evs] == [
        (None, "applied"),
        ("applied", "response_received"),
        ("response_received", "interview"),
        ("interview", "rejected"),
    ]
