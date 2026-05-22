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
