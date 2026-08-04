"""Live e2e for the extension features: company scan, save→board chain, and
the full save→score→tailor→autofill-attach chain (needs Ollama; auto-skipped
when the AI is down).

Run: pytest -m live tests/e2e/test_extension_features.py
"""

import uuid

import pytest
import requests

pytestmark = pytest.mark.live


def _cleanup_job(job_id: int):
    """Best-effort removal of the QA rows this test wrote into the real DB."""
    try:
        from db.database import get_session
        from db.models import Application, ApplicationEvent, Job, JobScore
        s = get_session()
        try:
            app = s.query(Application).filter_by(job_id=job_id).first()
            if app is not None:
                s.query(ApplicationEvent).filter_by(application_id=app.id).delete()
                s.delete(app)
            s.query(JobScore).filter_by(job_id=job_id).delete()
            job = s.query(Job).get(job_id)
            if job is not None:
                s.delete(job)
            s.commit()
        finally:
            s.close()
    except Exception as e:  # cleanup must never fail the test itself
        print(f"[cleanup] left QA rows for job {job_id}: {e}")


def test_company_scan_live_greenhouse(base_url):
    """Real board, real fetch: Anthropic's Greenhouse (already a scan target)."""
    r = requests.post(f"{base_url}/api/extension/company-scan",
                      json={"url": "https://job-boards.greenhouse.io/anthropic"},
                      timeout=90)
    d = r.json()
    assert d.get("ok"), d
    assert d["ats"] == "greenhouse"
    assert d["jobs_found"] >= 1
    scores = [j["score"] for j in d["jobs"]]
    assert scores == sorted(scores, reverse=True)
    for j in d["jobs"]:
        assert j["title"] and j["url"].startswith("http")
        assert 0 <= j["score"] <= 98


def test_company_scan_rejects_boardless_page(base_url):
    r = requests.post(f"{base_url}/api/extension/company-scan",
                      json={"url": "https://example.com/"}, timeout=60)
    d = r.json()
    assert not d.get("ok")
    assert "No supported job board" in d.get("error", "")


def test_save_dedupe_and_applied_board_chain(base_url):
    """The autofill→board chain the SW runs: save job, mark applied, verify."""
    tag = uuid.uuid4().hex[:10]
    url = f"https://boards.greenhouse.io/qaco/jobs/{tag}"
    job = {"url": url, "title": f"QA Data Analyst {tag}", "company": "QA Extension Co",
           "description": "Python and SQL analytics QA role.", "ensure_score": False}
    d = requests.post(f"{base_url}/api/extension/job", json=job, timeout=30).json()
    assert d["ok"] and d["created"], d
    job_id = d["job_id"]
    try:
        # idempotent save
        d2 = requests.post(f"{base_url}/api/extension/job", json=job, timeout=30).json()
        assert d2["job_id"] == job_id and not d2["created"]

        # the SW's post-autofill call
        a = requests.post(f"{base_url}/api/applied/record",
                          json={"url": url, "title": job["title"],
                                "company": job["company"],
                                "lead_source": "autofill", "source": "extension"},
                          timeout=30).json()
        assert a["ok"] and a["status"] == "applied" and a["job_id"] == job_id, a

        # recording again never regresses
        a2 = requests.post(f"{base_url}/api/applied/record",
                           json={"url": url, "source": "extension"}, timeout=30).json()
        assert a2["ok"] and a2["already"]

        # it shows up on the board
        detail = requests.get(f"{base_url}/api/application/{d['app_id']}", timeout=30)
        assert detail.status_code == 200
        assert "applied" in detail.text.lower()
    finally:
        _cleanup_job(job_id)


def _ollama_up(base_url) -> bool:
    try:
        h = requests.get(f"{base_url}/api/autofill/health", timeout=10).json()
        return bool(h.get("ollama_up"))
    except Exception:
        return False


def test_full_tailor_chain(base_url):
    """save → LLM score → tailored .docx → autofill serves the tailored file."""
    if not _ollama_up(base_url):
        pytest.skip("Ollama down — tailor chain needs the local LLM")

    tag = uuid.uuid4().hex[:10]
    company = f"QA Tailor Co {tag}"
    title = "Data Analyst"
    job = {
        "url": f"https://boards.greenhouse.io/qatailor/jobs/{tag}",
        "title": title, "company": company,
        "description": (
            "We are hiring a Data Analyst in San Francisco. You will build "
            "dashboards in Tableau, write SQL against PostgreSQL, run A/B tests, "
            "and automate reporting pipelines in Python (Pandas, NumPy). "
            "Experience with statistics, regression, and hypothesis testing "
            "required. Nice to have: LLM tooling, FastAPI, prompt engineering."),
        "ensure_score": True,
    }
    d = requests.post(f"{base_url}/api/extension/job", json=job, timeout=600).json()
    assert d["ok"], d
    job_id, app_id = d["job_id"], d["app_id"]
    try:
        assert d["scored"], f"scoring failed: {d.get('score_error')}"
        assert isinstance(d["fit_score"], int)

        t = requests.post(f"{base_url}/api/application/{app_id}/tailor",
                          timeout=600).json()
        assert t.get("ok"), t
        assert t["resume_filename"].endswith(".docx")

        # the seamless part: autofill now hands out the tailored résumé
        f = requests.get(f"{base_url}/api/autofill/resume_file",
                         params={"company": company, "job_title": title},
                         timeout=60)
        assert f.status_code == 200
        assert t["resume_filename"] in f.headers.get("content-disposition", "")
    finally:
        _cleanup_job(job_id)
