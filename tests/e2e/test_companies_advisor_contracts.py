"""Contract tests for companies/advisor/applied endpoints (live server)."""

import pytest
import requests

pytestmark = pytest.mark.live


def test_companies_list_shape(base_url):
    r = requests.get(f"{base_url}/api/companies", timeout=5)
    assert r.status_code == 200
    body = r.json()
    assert "companies" in body and "other" in body
    if body["companies"]:
        c = body["companies"][0]
        for key in ("id", "name", "counts", "why_fit_teaser", "draft_status"):
            assert key in c
        assert set(c["counts"]) == {"watching", "applied", "in_play", "closed"}


def test_company_detail_shape(base_url):
    companies = requests.get(f"{base_url}/api/companies", timeout=5).json()["companies"]
    if not companies:
        pytest.skip("no companies seeded")
    d = requests.get(f"{base_url}/api/company/{companies[0]['id']}", timeout=5).json()
    assert [g["stage"] for g in d["tree"]] == ["watching", "applied", "in_play", "closed"]
    for key in ("overview_md", "why_fit_md", "hiring_bar_md", "notes_md"):
        assert key in d


def test_advisor_report_shape(base_url):
    r = requests.get(f"{base_url}/api/advisor/report?since=2026-01-01", timeout=10)
    assert r.status_code == 200
    body = r.json()
    assert set(body["stats"]) == {"applied", "responses", "interviews", "closed"}
    assert isinstance(body["companies"], list)


def test_advisor_report_validates_since(base_url):
    assert requests.get(f"{base_url}/api/advisor/report?since=bad", timeout=5).status_code == 400


def test_applied_record_requires_url(base_url):
    r = requests.post(f"{base_url}/api/applied/record", json={}, timeout=5)
    assert r.status_code == 422   # pydantic validation
