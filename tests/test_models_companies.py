"""Schema smoke tests for Company / ApplicationEvent and new columns."""

from datetime import datetime, timezone

from db.models import (
    Application,
    ApplicationEvent,
    ApplicationStatus,
    Company,
    Job,
)


def _mk_job(session, company="Chime Financial, Inc", url="https://x.test/1"):
    job = Job(title="AI/ML Engineer", company=company, url=url,
              source="test", dedup_hash=url)
    session.add(job)
    session.flush()
    return job


def test_company_row_roundtrip(tmp_session):
    c = Company(name="Chime", name_normalized="chime",
                careers_url="https://careers.chime.com",
                ats_platform="greenhouse", priority="high", status="target",
                overview_md="SF consumer fintech.", why_fit_md="Fits.",
                hiring_bar_md="1-2 YOE ok.", profile_source="seeded")
    tmp_session.add(c)
    tmp_session.commit()
    got = tmp_session.query(Company).filter_by(name_normalized="chime").one()
    assert got.suggested is False
    assert got.draft_status is None
    assert got.notes_md is None


def test_job_links_to_company(tmp_session):
    c = Company(name="Chime", name_normalized="chime")
    tmp_session.add(c)
    tmp_session.flush()
    job = _mk_job(tmp_session)
    job.company_id = c.id
    tmp_session.commit()
    assert tmp_session.query(Job).one().company_id == c.id


def test_application_event_rows(tmp_session):
    job = _mk_job(tmp_session)
    app = Application(job_id=job.id, status=ApplicationStatus.FOUND,
                      lead_source="Bianca / Vantage Point")
    tmp_session.add(app)
    tmp_session.flush()
    ev = ApplicationEvent(application_id=app.id,
                          occurred_at=datetime.now(timezone.utc),
                          from_status="found", to_status="applied",
                          source="dashboard")
    tmp_session.add(ev)
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 1
    assert app.next_action is None
