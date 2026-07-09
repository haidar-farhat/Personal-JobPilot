"""record_status_change — single funnel for Application.status writes."""

from datetime import datetime, timezone

from db.database import record_status_change
from db.models import Application, ApplicationEvent, ApplicationStatus, Job


def _mk_app(session, status=ApplicationStatus.FOUND):
    job = Job(title="T", company="Plaid", url="https://x.test/1",
              source="test", dedup_hash="h1")
    session.add(job)
    session.flush()
    app = Application(job_id=job.id, status=status)
    session.add(app)
    session.flush()
    return app


def test_sets_status_dates_and_event(tmp_session):
    app = _mk_app(tmp_session)
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED,
                         source="dashboard", note="via test")
    tmp_session.commit()
    assert app.status == ApplicationStatus.APPLIED
    assert app.date_applied is not None
    ev = tmp_session.query(ApplicationEvent).one()
    assert (ev.from_status, ev.to_status, ev.source, ev.note) == \
        ("found", "applied", "dashboard", "via test")


def test_interview_and_response_stamp_their_dates(tmp_session):
    app = _mk_app(tmp_session, ApplicationStatus.APPLIED)
    record_status_change(tmp_session, app, ApplicationStatus.RESPONSE_RECEIVED,
                         source="dashboard")
    assert app.response_date is not None
    record_status_change(tmp_session, app, ApplicationStatus.INTERVIEW,
                         source="dashboard")
    assert app.interview_date is not None
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 2


def test_date_applied_survives_status_regression_and_reentry(tmp_session):
    app = _mk_app(tmp_session)
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED, source="dashboard")
    tmp_session.commit()   # persist; reload is naive (SQLite drops tzinfo)
    first_stamp = app.date_applied
    record_status_change(tmp_session, app, ApplicationStatus.REJECTED, source="dashboard")
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED, source="dashboard")
    tmp_session.commit()
    assert app.date_applied == first_stamp          # original timestamp preserved
    assert tmp_session.query(ApplicationEvent).count() == 3


def test_same_status_is_noop(tmp_session):
    app = _mk_app(tmp_session, ApplicationStatus.APPLIED)
    record_status_change(tmp_session, app, ApplicationStatus.APPLIED,
                         source="extension")
    tmp_session.commit()
    assert tmp_session.query(ApplicationEvent).count() == 0
