"""/api/extension — board detection, sniffing, résumé-relative ranking,
find-or-import (+ optional scoring) for the extension's scan/tailor features."""

import pytest
from fastapi.testclient import TestClient

import server.extension_api as ext
import server.dashboard as dash
from agents.scanner.base import RawJob
from db.models import Application, ApplicationStatus, Job, JobScore

client = TestClient(dash.app)


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, session_factory):
    monkeypatch.setattr(ext, "get_session", session_factory)


# ============================================================
# detect_board
# ============================================================

@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/anthropic", ("greenhouse", "anthropic")),
    ("https://job-boards.greenhouse.io/openaisf/jobs/123", ("greenhouse", "openaisf")),
    ("https://boards.greenhouse.io/embed/job_board?for=acme&b=x", ("greenhouse", "acme")),
    ("https://jobs.lever.co/scaleai/8a4f2c", ("lever", "scaleai")),
    ("https://jobs.ashbyhq.com/sierra/some-role-id", ("ashby", "sierra")),
    ("https://apply.workable.com/huggingface/j/ABC123/", ("workable", "huggingface")),
    ("https://acme.workable.com/jobs/1", ("workable", "acme")),
    ("https://jobs.smartrecruiters.com/Visa/744000064", ("smartrecruiters", "Visa")),
])
def test_detect_board(url, expected):
    assert ext.detect_board(url) == expected


def test_detect_board_workday_skips_locale_segment():
    ats, info = ext.detect_board(
        "https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/x")
    assert ats == "workday"
    assert info == {"host": "nvidia.wd5.myworkdayjobs.com",
                    "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"}


@pytest.mark.parametrize("url", [
    "https://example.com/careers",
    "https://boards.greenhouse.io",          # no slug
    "not a url at all",
])
def test_detect_board_none(url):
    assert ext.detect_board(url) is None


# ============================================================
# sniff_board
# ============================================================

def test_sniff_board_greenhouse_embed():
    html = '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
    assert ext.sniff_board(html) == ("greenhouse", "acme")


def test_sniff_board_majority_wins():
    html = ('<a href="https://jobs.lever.co/acme/1">a</a>'
            '<a href="https://jobs.lever.co/acme/2">b</a>'
            '<a href="https://boards.greenhouse.io/other">c</a>')
    assert ext.sniff_board(html) == ("lever", "acme")


def test_sniff_board_none():
    assert ext.sniff_board("<html><body>just a homepage</body></html>") is None
    assert ext.sniff_board("") is None


# ============================================================
# score_roles
# ============================================================

def _rj(title, desc="", remote=False):
    return RawJob(title=title, company="Acme", location="San Francisco, CA",
                  url=f"https://acme.example/{title.replace(' ', '-')}",
                  source="test", description=desc, is_remote=remote)


TERMS = ["Python", "SQL", "LLM", "FastAPI", "Tableau", "prompt engineering"]
ROLES = ["Data Analyst", "AI Engineer", "Machine Learning Engineer"]


def test_score_roles_orders_by_resume_match():
    ranked = ext.score_roles([
        _rj("Office Manager", "Order snacks and manage calendars"),
        _rj("Data Analyst", "Python SQL Tableau dashboards and statistics"),
        _rj("AI Engineer", "LLM apps, prompt engineering, Python, FastAPI"),
    ], resume_terms=TERMS, role_keywords=ROLES)
    titles = [j["title"] for j in ranked]
    assert titles[-1] == "Office Manager"
    assert set(titles[:2]) == {"Data Analyst", "AI Engineer"}
    scores = [j["score"] for j in ranked]
    assert scores == sorted(scores, reverse=True)
    assert ranked[-1]["score"] < ranked[0]["score"]


def test_score_roles_explains_matches():
    ranked = ext.score_roles(
        [_rj("AI Engineer", "You will build LLM tools in Python.")],
        resume_terms=TERMS, role_keywords=ROLES)
    j = ranked[0]
    assert j["role_match"] == "AI Engineer"
    assert {"LLM", "Python"} <= set(j["matched"])
    assert j["snippet"].startswith("You will build")


def test_score_roles_word_boundaries():
    # "SQL" must not match inside "NoSQLite"; "Data Analyst" not inside "Database"
    ranked = ext.score_roles(
        [_rj("Database Administrator", "NoSQLite experience required")],
        resume_terms=["SQL"], role_keywords=["Data Analyst"])
    assert ranked[0]["role_match"] is None
    assert ranked[0]["matched"] == []


def test_score_roles_default_config_loads():
    # uses real config/base_resume.yaml + settings.yaml — must not blow up
    ranked = ext.score_roles([_rj("AI Engineer", "Python, LLM, FastAPI")])
    assert ranked and ranked[0]["score"] > 5


# ============================================================
# POST /api/extension/company-scan
# ============================================================

def test_company_scan_detected_board(monkeypatch):
    monkeypatch.setattr(ext, "_fetch_board_jobs", lambda ats, slug, cfg: [
        _rj("Data Analyst", "Python SQL Tableau"),
        _rj("Office Manager"),
    ])
    r = client.post("/api/extension/company-scan",
                    json={"url": "https://boards.greenhouse.io/acme"})
    d = r.json()
    assert d["ok"] and d["ats"] == "greenhouse"
    assert d["jobs_found"] == 2
    assert d["jobs"][0]["title"] == "Data Analyst"
    assert d["company"] == "Acme"


def test_company_scan_sniffs_company_page(monkeypatch):
    class FakeResp:
        text = '<a href="https://jobs.lever.co/acme/1">Jobs</a>'
        def raise_for_status(self): pass
    monkeypatch.setattr(ext.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(ext, "_fetch_board_jobs", lambda ats, slug, cfg: [
        _rj("AI Engineer", "LLM Python")])
    r = client.post("/api/extension/company-scan",
                    json={"url": "https://acme.example/careers"})
    d = r.json()
    assert d["ok"] and d["ats"] == "lever" and d["jobs"]


def test_company_scan_no_board_found(monkeypatch):
    class FakeResp:
        text = "<html>no ats here</html>"
        def raise_for_status(self): pass
    monkeypatch.setattr(ext.requests, "get", lambda *a, **k: FakeResp())
    r = client.post("/api/extension/company-scan",
                    json={"url": "https://acme.example/"})
    d = r.json()
    assert not d["ok"] and "No supported job board" in d["error"]


def test_company_scan_unreachable_page(monkeypatch):
    def boom(*a, **k):
        raise ext.requests.RequestException("dns fail")
    monkeypatch.setattr(ext.requests, "get", boom)
    r = client.post("/api/extension/company-scan",
                    json={"url": "https://nope.example/"})
    d = r.json()
    assert not d["ok"] and "Could not fetch page" in d["error"]


def test_company_scan_respects_limit(monkeypatch):
    monkeypatch.setattr(ext, "_fetch_board_jobs", lambda ats, slug, cfg: [
        _rj(f"Data Analyst {i}", "Python SQL") for i in range(10)])
    r = client.post("/api/extension/company-scan",
                    json={"url": "https://boards.greenhouse.io/acme", "limit": 3})
    d = r.json()
    assert d["jobs_found"] == 10 and len(d["jobs"]) == 3


# ============================================================
# POST /api/extension/job
# ============================================================

JOB = {"url": "https://boards.greenhouse.io/acme/jobs/1",
       "title": "Data Analyst", "company": "Acme",
       "description": "Python and SQL analytics role.",
       "ensure_score": False}


def test_save_job_creates_job_and_application(session_factory):
    d = client.post("/api/extension/job", json=JOB).json()
    assert d["ok"] and d["created"] and not d["scored"]
    s = session_factory()
    job = s.query(Job).get(d["job_id"])
    app = s.query(Application).get(d["app_id"])
    assert job.title == "Data Analyst" and job.source == "extension"
    assert job.description == "Python and SQL analytics role."
    assert app.job_id == job.id and app.status == ApplicationStatus.FOUND
    s.close()


def test_save_job_dedupes_by_url():
    first = client.post("/api/extension/job", json=JOB).json()
    second = client.post("/api/extension/job", json=JOB).json()
    assert second["job_id"] == first["job_id"]
    assert second["app_id"] == first["app_id"]
    assert not second["created"]


def test_save_job_backfills_description(session_factory):
    bare = {**JOB, "description": ""}
    d1 = client.post("/api/extension/job", json=bare).json()
    d2 = client.post("/api/extension/job", json=JOB).json()
    assert d2["job_id"] == d1["job_id"]
    s = session_factory()
    assert s.query(Job).get(d1["job_id"]).description == JOB["description"]
    s.close()


def test_save_job_ensure_score_runs_ranker(monkeypatch, session_factory):
    def fake_score_job(job, write_eval_report=True):
        return JobScore(job_id=job.id, fit_score=77, recommended_action="apply")
    import agents.ranker
    monkeypatch.setattr(agents.ranker, "score_job", fake_score_job)
    d = client.post("/api/extension/job", json={**JOB, "ensure_score": True}).json()
    assert d["ok"] and d["scored"] and d["fit_score"] == 77
    s = session_factory()
    assert s.query(JobScore).filter_by(job_id=d["job_id"]).one().fit_score == 77
    s.close()


def test_save_job_score_failure_is_soft(monkeypatch):
    def boom(job, write_eval_report=True):
        raise RuntimeError("ollama down")
    import agents.ranker
    monkeypatch.setattr(agents.ranker, "score_job", boom)
    d = client.post("/api/extension/job", json={**JOB, "ensure_score": True}).json()
    assert d["ok"] and not d["scored"]
    assert "ollama down" in d["score_error"]


def test_save_job_reuses_existing_score(monkeypatch, session_factory):
    d1 = client.post("/api/extension/job", json=JOB).json()
    s = session_factory()
    s.add(JobScore(job_id=d1["job_id"], fit_score=88, recommended_action="apply"))
    s.commit()
    s.close()
    def boom(job, write_eval_report=True):
        raise AssertionError("must not re-score")
    import agents.ranker
    monkeypatch.setattr(agents.ranker, "score_job", boom)
    d2 = client.post("/api/extension/job", json={**JOB, "ensure_score": True}).json()
    assert d2["scored"] and d2["fit_score"] == 88
