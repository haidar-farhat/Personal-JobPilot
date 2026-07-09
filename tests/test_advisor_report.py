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
    # pipeline-only app: its ONLY in-window event is a non-milestone
    # (scored) — must never surface in the companies sections
    j3 = Job(title="Watcher role", company="Plaid", url="https://p.test/3",
             source="test", dedup_hash="p3", company_id=c.id)
    s.add(j3); s.flush()
    a3 = Application(job_id=j3.id, status=ApplicationStatus.SCORED)
    s.add(a3); s.flush()
    ev(a3.id, 3, None, "scored", source="backfill")
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


def test_non_milestone_apps_excluded(session_factory):
    """Apps whose only in-window events are pipeline churn (found/scored/...)
    stay out of the sections — hundreds of backfilled scored events must not
    drown the advisor report. Stats are untouched by their exclusion."""
    _seed(session_factory)
    r = client.get("/api/advisor/report?since=2026-07-02")
    body = r.json()
    assert body["stats"] == {"applied": 1, "responses": 1, "interviews": 1,
                             "closed": 0}
    all_titles = [a["title"] for sec in body["companies"]
                  for a in sec["applications"]]
    assert "Watcher role" not in all_titles


def test_name_variants_group_to_one_section(session_factory):
    """Unlinked jobs whose company strings normalize identically ("Brex" /
    "Brex, Inc.") land in ONE section, not two."""
    s = session_factory()
    def mk(title, company, dh, day):
        j = Job(title=title, company=company, url=f"https://b.test/{dh}",
                source="test", dedup_hash=dh)
        s.add(j); s.flush()
        a = Application(job_id=j.id, status=ApplicationStatus.APPLIED)
        s.add(a); s.flush()
        s.add(ApplicationEvent(application_id=a.id,
                               occurred_at=datetime(2026, 7, day, tzinfo=timezone.utc),
                               from_status="scored", to_status="applied",
                               source="dashboard"))
    mk("Role A", "Brex", "b1", 3)
    mk("Role B", "Brex, Inc.", "b2", 4)
    s.commit(); s.close()
    r = client.get("/api/advisor/report?since=2026-07-01")
    body = r.json()
    assert len(body["companies"]) == 1
    assert len(body["companies"][0]["applications"]) == 2


def test_reapply_counts_distinct_apps(session_factory):
    """Stats count distinct applications, not events — an app that re-enters
    applied (applied -> rejected -> applied) is ONE application sent."""
    s = session_factory()
    j = Job(title="Boomerang", company="Ramp", url="https://r.test/1",
            source="test", dedup_hash="r1")
    s.add(j); s.flush()
    a = Application(job_id=j.id, status=ApplicationStatus.APPLIED)
    s.add(a); s.flush()
    for day, frm, to in ((2, "scored", "applied"),
                         (3, "applied", "rejected"),
                         (4, "rejected", "applied")):
        s.add(ApplicationEvent(application_id=a.id,
                               occurred_at=datetime(2026, 7, day, tzinfo=timezone.utc),
                               from_status=frm, to_status=to, source="dashboard"))
    s.commit(); s.close()
    r = client.get("/api/advisor/report?since=2026-07-01")
    assert r.json()["stats"] == {"applied": 1, "responses": 0, "interviews": 0,
                                 "closed": 1}


def test_lazy_link_attaches_unlinked_job_to_company(session_factory):
    """advisor_report runs the shared linker (db.company_linking) before
    building the report, same as companies.py's _lazy_link — an unlinked
    "Brex, Inc." job rolls up under the existing "Brex" Company's canonical
    name, and its company_id is set for subsequent reads."""
    s = session_factory()
    c = Company(name="Brex", name_normalized="brex")
    s.add(c); s.flush()
    j = Job(title="Platform Eng", company="Brex, Inc.", url="https://brex.test/1",
            source="test", dedup_hash="brex1")   # unlinked: no company_id set
    s.add(j); s.flush()
    a = Application(job_id=j.id, status=ApplicationStatus.APPLIED)
    s.add(a); s.flush()
    s.add(ApplicationEvent(application_id=a.id,
                           occurred_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
                           from_status="scored", to_status="applied", source="dashboard"))
    company_id, job_id = c.id, j.id
    s.commit(); s.close()

    r = client.get("/api/advisor/report?since=2026-07-01")
    assert r.status_code == 200
    body = r.json()
    assert [sec["company"] for sec in body["companies"]] == ["Brex"]

    check = session_factory()
    assert check.query(Job).get(job_id).company_id == company_id
    check.close()
