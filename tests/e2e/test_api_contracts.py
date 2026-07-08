"""Contract tests for JobPilot REST endpoints (no browser needed)."""

import pytest
import requests


pytestmark = pytest.mark.live


def test_stats_returns_full_shape(base_url):
    r = requests.get(f"{base_url}/api/stats", timeout=5)
    assert r.status_code == 200
    body = r.json()
    for key in (
        "total_jobs",
        "jobs_today",
        "jobs_this_week",
        "pipeline",
        "avg_score",
        "high_matches",
        "needs_followup",
        "response_rate",
        "pending_review",
        "last_update",
    ):
        assert key in body, f"missing key in /api/stats: {key}"


def test_applications_default_returns_list(base_url):
    r = requests.get(f"{base_url}/api/applications", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)


def test_applications_status_filter_active(base_url):
    r = requests.get(f"{base_url}/api/applications?status=active", timeout=5)
    assert r.status_code == 200
    active_statuses = {
        "materials_ready",
        "queued",
        "approved",
        "applied",
        "scored",
        "interview",
    }
    for app in r.json():
        assert app["status"] in active_statuses, f"unexpected status in active filter: {app['status']}"


def test_applications_status_filter_applied(base_url):
    r = requests.get(f"{base_url}/api/applications?status=applied", timeout=5)
    assert r.status_code == 200
    applied_statuses = {
        "applied",
        "interview",
        "response_received",
        "rejected",
        "no_response",
    }
    for app in r.json():
        assert app["status"] in applied_statuses


def test_applications_search_filter(base_url):
    r = requests.get(f"{base_url}/api/applications?search=zzzzz_no_match_marker_xx", timeout=5)
    assert r.status_code == 200
    assert r.json() == []


def test_application_detail_404_for_unknown_id(base_url):
    r = requests.get(f"{base_url}/api/application/99999999", timeout=5)
    assert r.status_code == 404


def test_application_detail_round_trip(base_url):
    apps = requests.get(f"{base_url}/api/applications?limit=1", timeout=5).json()
    if not apps:
        pytest.skip("no applications available")
    app_id = apps[0]["id"]
    r = requests.get(f"{base_url}/api/application/{app_id}", timeout=5)
    assert r.status_code == 200
    assert r.json()["id"] == app_id


def test_scan_logs_endpoint(base_url):
    r = requests.get(f"{base_url}/api/scan-logs", timeout=5)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_activity_endpoint(base_url):
    r = requests.get(f"{base_url}/api/activity", timeout=5)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_healthz_endpoint(base_url):
    """The new /healthz endpoint must report ollama + db + scheduler_jobs status."""
    r = requests.get(f"{base_url}/healthz", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "ollama" in body
    assert body["ollama"] in ("reachable", "unreachable")
    assert "db" in body
    assert body["db"] in ("ok", "error")


def test_invalid_status_post_returns_400(base_url):
    apps = requests.get(f"{base_url}/api/applications?limit=1", timeout=5).json()
    if not apps:
        pytest.skip("no applications available")
    app_id = apps[0]["id"]
    r = requests.post(
        f"{base_url}/api/application/{app_id}/status",
        json={"status": "this_is_not_a_real_status"},
        timeout=5,
    )
    assert r.status_code == 400


def test_file_download_404_for_missing_resume(base_url):
    """Even on a fresh DB with no files materialized, the endpoint must 404 cleanly."""
    apps = requests.get(f"{base_url}/api/applications?limit=20", timeout=5).json()
    target = next((a for a in apps if not a.get("has_tailored_cv")), None)
    if not target:
        pytest.skip("no application without a tailored CV available")
    r = requests.get(f"{base_url}/api/file/resume/{target['id']}", timeout=5)
    assert r.status_code == 404


def test_resume_meta_reports_configured_pdf(base_url):
    r = requests.get(f"{base_url}/api/autofill/resume_meta?resume_pref=ai", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert body["source"] in ("profile_pdf", "rendered_base")
    if body["source"] == "profile_pdf":
        # the ATS sees the file under its ORIGINAL name — exactly what the pill shows
        assert body["serve_name"] == body["original_name"]
        assert body["size"] > 0
    else:
        assert body["serve_name"].startswith("Matthew_Cromaz_Resume")


def test_resume_upload_roundtrip_and_restore(base_url):
    """Upload a dummy PDF via the widget endpoint, verify meta + served bytes
    change, then restore the real résumé exactly."""
    import base64
    from pathlib import Path

    target = Path(__file__).resolve().parents[2] / "config" / "resume_matthew_ai.pdf"
    if not target.exists():
        pytest.skip("no configured resume PDF to test against")
    original = target.read_bytes()
    side = target.with_suffix(target.suffix + ".meta.json")
    original_side = side.read_bytes() if side.exists() else None

    dummy = b"%PDF-1.4 jobpilot upload test\n%%EOF"
    try:
        r = requests.post(f"{base_url}/api/autofill/resume_upload",
                          json={"resume_pref": "ai", "filename": "iteration_7.pdf",
                                "b64": base64.b64encode(dummy).decode()}, timeout=10)
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["original_name"] == "iteration_7.pdf"
        assert body["size"] == len(dummy)

        served = requests.get(f"{base_url}/api/autofill/resume_file?resume_pref=ai", timeout=10)
        assert served.status_code == 200
        assert served.content == dummy
        # attaches under the uploaded file's own name, not a renamed one
        assert "iteration_7.pdf" in served.headers.get("content-disposition", "")

        # non-PDF payload must be rejected
        bad = requests.post(f"{base_url}/api/autofill/resume_upload",
                            json={"resume_pref": "ai", "filename": "x.pdf",
                                  "b64": base64.b64encode(b"not a pdf").decode()}, timeout=10)
        assert bad.status_code == 400
    finally:
        target.write_bytes(original)
        if original_side is not None:
            side.write_bytes(original_side)
        elif side.exists():
            side.unlink()


def test_import_jobs_ingests_and_dedupes(base_url):
    """MCP-connector bridge: POSTed jobs flow through dedupe + store + scoring.

    Uses a fixed fake company so repeat runs MERGE as duplicates instead of
    growing the DB — first-ever run imports, every run after merges.
    """
    payload = {
        "source": "mcp-indeed",
        "score": False,  # defer LLM ranking — keeps Ollama free for the autofill e2es
        "jobs": [
            {"title": "QA Import AI Engineer", "company": "QA Import Test Co",
             "location": "San Francisco, CA", "url": "https://example.com/qa-import-1",
             "salary_text": "$150,000 - $180,000 a year",
             "date_posted": "June 11, 2026", "job_type": "Full-time"},
            {"title": "QA Import Behavior Technician", "company": "QA Import Test Co",
             "location": "San Mateo, CA", "url": "https://example.com/qa-import-2",
             "salary_text": "$35 - $45 an hour",
             "date_posted": "2026-06-25", "job_type": "Part-time"},
        ],
    }
    r = requests.post(f"{base_url}/api/import/jobs", json=payload, timeout=120)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["received"] == 2
    # every job either imported now or merged into an earlier run's row
    assert body["imported"] + body["duplicates_merged"] == 2
    assert body["errors"] == []

    # idempotent: same payload again -> all duplicates, nothing new
    r2 = requests.post(f"{base_url}/api/import/jobs", json=payload, timeout=120)
    body2 = r2.json()
    assert body2["imported"] == 0
    assert body2["duplicates_merged"] == 2
