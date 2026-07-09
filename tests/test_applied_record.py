"""/api/applied/record — match-or-create + idempotent applied marking."""

import hashlib

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


def test_matches_job_with_tracking_params(session_factory):
    """Branch (b): canonical hostname+path match ignoring query/fragment."""
    jid = _mk_job(session_factory)
    r = client.post("/api/applied/record", json={
        "url": ("https://job-boards.greenhouse.io/affirm/jobs/7485068003"
                "?gh_src=abc123&utm_source=google#app"),
        "source": "extension"})
    body = r.json()
    assert body["ok"] and body["matched"] and not body["created"]
    s = session_factory()
    assert s.query(Job).count() == 1          # no duplicate Job created
    app = s.query(Application).filter_by(job_id=jid).one()
    assert app.status == ApplicationStatus.APPLIED
    s.close()


def test_matches_by_title_and_company(session_factory):
    """Branch (c): normalized company + casefolded title equality."""
    s = session_factory()
    job = Job(title="Data Engineer", company="Brex Financial",
              url="https://example.com/careers/de-1", source="lever",
              dedup_hash="h-brex-1")
    s.add(job)
    s.flush()
    s.add(Application(job_id=job.id, status=ApplicationStatus.SCORED))
    s.commit()
    jid = job.id
    s.close()
    r = client.post("/api/applied/record", json={
        "url": "https://boards.example.net/other/999",
        "title": "data engineer ", "company": "Brex",
        "source": "dashboard"})
    body = r.json()
    assert body["ok"] and body["matched"] and not body["created"]
    s = session_factory()
    assert s.query(Job).count() == 1          # no duplicate Job created
    app = s.query(Application).filter_by(job_id=jid).one()
    assert app.status == ApplicationStatus.APPLIED
    s.close()


def test_empty_url_rejected():
    r = client.post("/api/applied/record", json={"url": "", "source": "dashboard"})
    assert r.status_code == 422


def test_bad_source_rejected():
    r = client.post("/api/applied/record", json={
        "url": "https://jobs.example.com/posting/1", "source": "webhook"})
    assert r.status_code == 422


def test_integrity_race_recovers(session_factory):
    """Create path hits UNIQUE(dedup_hash) -> rollback + re-match, not a 500."""
    url = "https://jobs.example.com/posting/xyz"
    s = session_factory()
    job = Job(title="Ops Analyst", company="Elsewhere",
              url="https://elsewhere.example.net/j/1", source="lever",
              dedup_hash=hashlib.sha256(url.encode()).hexdigest())
    s.add(job)
    s.commit()
    jid = job.id
    s.close()
    r = client.post("/api/applied/record", json={"url": url, "source": "extension"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and not body["created"]
    assert body["job_id"] == jid
    s = session_factory()
    assert s.query(Job).count() == 1          # our rolled-back insert left nothing
    assert s.query(Application).one().status == ApplicationStatus.APPLIED
    s.close()


def test_backfills_lead_source_on_already_path(session_factory):
    """Fix 2: already-applied rows with NULL lead_source get it backfilled."""
    s = session_factory()
    job = Job(title="SWE", company="Affirm",
              url="https://job-boards.greenhouse.io/affirm/jobs/1",
              source="greenhouse", dedup_hash="h-affirm-2")
    s.add(job)
    s.flush()
    s.add(Application(job_id=job.id, status=ApplicationStatus.APPLIED))
    s.commit()
    s.close()
    r = client.post("/api/applied/record", json={
        "url": "https://job-boards.greenhouse.io/affirm/jobs/1",
        "lead_source": "Bianca / Vantage Point", "source": "dashboard"})
    assert r.json()["already"] is True
    s = session_factory()
    assert s.query(Application).one().lead_source == "Bianca / Vantage Point"
    assert s.query(ApplicationEvent).count() == 0   # still no event on already path
    s.close()
