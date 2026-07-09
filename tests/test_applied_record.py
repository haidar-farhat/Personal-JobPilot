"""/api/applied/record — match-or-create + idempotent applied marking."""

import pytest
from fastapi.testclient import TestClient

import server.applied as applied_mod
import server.dashboard as dash
from db.models import Application, ApplicationEvent, ApplicationStatus, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(applied_mod, "get_session", session_factory)


def _mk_job(session_factory, url="https://job-boards.greenhouse.io/affirm/jobs/7485068003"):
    s = session_factory()
    job = Job(title="SWE, Early Career", company="Affirm", url=url,
              source="greenhouse", dedup_hash="h-affirm-1")
    s.add(job)
    s.flush()
    app = Application(job_id=job.id, status=ApplicationStatus.SCORED)
    s.add(app)
    s.commit()
    jid = job.id
    s.close()
    return jid


def test_matches_existing_job_by_url(session_factory):
    jid = _mk_job(session_factory)
    r = client.post("/api/applied/record", json={
        "url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
        "source": "extension"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["matched"] and not body["created"]
    s = session_factory()
    app = s.query(Application).filter_by(job_id=jid).one()
    assert app.status == ApplicationStatus.APPLIED
    assert app.lead_source == "extension"
    ev = s.query(ApplicationEvent).one()
    assert (ev.to_status, ev.source) == ("applied", "extension")
    s.close()


def test_idempotent_second_call(session_factory):
    _mk_job(session_factory)
    payload = {"url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
               "source": "extension"}
    client.post("/api/applied/record", json=payload)
    r2 = client.post("/api/applied/record", json=payload)
    assert r2.json()["already"] is True
    s = session_factory()
    assert s.query(ApplicationEvent).count() == 1   # no duplicate event
    s.close()


def test_never_regresses_past_applied(session_factory):
    jid = _mk_job(session_factory)
    s = session_factory()
    app = s.query(Application).filter_by(job_id=jid).one()
    app.status = ApplicationStatus.INTERVIEW
    s.commit(); s.close()
    r = client.post("/api/applied/record", json={
        "url": "https://job-boards.greenhouse.io/affirm/jobs/7485068003",
        "source": "dashboard"})
    assert r.json()["already"] is True
    s = session_factory()
    assert s.query(Application).one().status == ApplicationStatus.INTERVIEW
    s.close()


def test_creates_minimal_record_when_no_match(session_factory):
    r = client.post("/api/applied/record", json={
        "url": "https://jobs.example.com/posting/123",
        "title": "Data Engineer", "company": "Brex",
        "lead_source": "Bianca / Vantage Point", "source": "dashboard"})
    body = r.json()
    assert body["ok"] and body["created"]
    s = session_factory()
    job = s.query(Job).one()
    assert (job.title, job.company) == ("Data Engineer", "Brex")
    app = s.query(Application).one()
    assert app.status == ApplicationStatus.APPLIED
    assert app.lead_source == "Bianca / Vantage Point"
    s.close()


def test_company_falls_back_to_hostname(session_factory):
    r = client.post("/api/applied/record", json={
        "url": "https://careers.chime.com/jobs/1/x", "source": "extension"})
    assert r.json()["created"]
    s = session_factory()
    assert s.query(Job).one().company == "careers.chime.com"
    s.close()
