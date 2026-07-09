"""Advisor report — since-date aggregation from application_events."""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import server.advisor as advisor_mod
import server.dashboard as dash
from db.models import Application, ApplicationEvent, ApplicationStatus, Company, Job

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(advisor_mod, "get_session", session_factory)


def _seed(session_factory):
    s = session_factory()
    c = Company(name="Plaid", name_normalized="plaid")
    s.add(c); s.flush()
    def ev(app_id, day, frm, to, source="dashboard"):
        s.add(ApplicationEvent(application_id=app_id,
                               occurred_at=datetime(2026, 7, day, tzinfo=timezone.utc),
                               from_status=frm, to_status=to, source=source))
    j1 = Job(title="SWE Backend", company="Plaid", url="https://p.test/1",
             source="test", dedup_hash="p1", company_id=c.id)
    s.add(j1); s.flush()
    a1 = Application(job_id=j1.id, status=ApplicationStatus.INTERVIEW,
                     lead_source="Bianca / Vantage Point")
    s.add(a1); s.flush()
    ev(a1.id, 2, "scored", "applied")
    ev(a1.id, 6, "applied", "response_received")
    ev(a1.id, 7, "response_received", "interview")
    j2 = Job(title="Old role", company="Plaid", url="https://p.test/2",
             source="test", dedup_hash="p2", company_id=c.id)
    s.add(j2); s.flush()
    a2 = Application(job_id=j2.id, status=ApplicationStatus.APPLIED)
    s.add(a2); s.flush()
    ev(a2.id, 1, None, "applied", source="backfill")   # before the window
    s.commit(); s.close()


def test_report_counts_and_sections(session_factory):
    _seed(session_factory)
    r = client.get("/api/advisor/report?since=2026-07-02")
    assert r.status_code == 200
    body = r.json()
    assert body["stats"] == {"applied": 1, "responses": 1, "interviews": 1,
                             "closed": 0}
    plaid = next(sec for sec in body["companies"] if sec["company"] == "Plaid")
    titles = [a["title"] for a in plaid["applications"]]
    assert "SWE Backend" in titles and "Old role" not in titles
    swe = next(a for a in plaid["applications"] if a["title"] == "SWE Backend")
    assert swe["lead_source"] == "Bianca / Vantage Point"
    assert [e["to"] for e in swe["events"]] == ["applied", "response_received", "interview"]


def test_report_bad_since_400(session_factory):
    assert client.get("/api/advisor/report?since=nonsense").status_code == 400


def test_backfill_events_marked_approx(session_factory):
    _seed(session_factory)
    r = client.get("/api/advisor/report?since=2026-06-01")   # includes the backfill event
    body = r.json()
    plaid = next(sec for sec in body["companies"] if sec["company"] == "Plaid")
    old = next(a for a in plaid["applications"] if a["title"] == "Old role")
    assert old["events"][0]["approx"] is True
