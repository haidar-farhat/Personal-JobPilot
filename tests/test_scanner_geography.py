"""Scanners must NOT drop jobs for being outside the Bay Area (2026-08-03).

Until this date every ATS scanner applied a hardcoded `bay_area_keywords`
allow-list and discarded non-matching jobs *at ingest*, before storage. That
silently defeated the relocation pivot: out-of-state companies were added to
target_companies.yaml but only their "remote"-labelled postings ever survived.

Geography now belongs to two config-driven layers instead — the non-US gate in
BaseScanner.run() and the ranker's location_remote dimension. These tests pin
that behaviour so the allow-list doesn't creep back in.
"""

import json
from types import SimpleNamespace

import pytest
import yaml
from pathlib import Path

from agents.scanner.ashby import AshbyScanner
from agents.scanner.career_pages import GreenhouseScanner, LeverScanner

CONFIG = yaml.safe_load(
    (Path(__file__).parent.parent / "config" / "settings.yaml").read_text(encoding="utf-8")
)

# Locations that the old allow-list would have thrown away.
OUT_OF_BAY = [
    "Scottsdale, Arizona, United States",
    "Austin, Texas",
    "Denver, Colorado",
    "Portland, OR",
    "Las Vegas, NV",
    "Raleigh, North Carolina",
    "South Jordan, UT",
    "Nashville, TN",
    "Tampa, FL",
]


def _fake_response(payload):
    return SimpleNamespace(json=lambda: payload, text=json.dumps(payload))


@pytest.mark.parametrize("location", OUT_OF_BAY)
def test_greenhouse_keeps_out_of_bay(monkeypatch, location):
    s = GreenhouseScanner(CONFIG)
    payload = {"jobs": [{
        "title": "Data Analyst",
        "location": {"name": location},
        "absolute_url": "https://example.com/job/1",
        "content": "Analytics role.",
    }]}
    monkeypatch.setattr(s, "_safe_request", lambda *a, **k: _fake_response(payload))

    jobs = s._scan_company({"name": "Acme", "api_url": "https://x", "location": location})
    assert len(jobs) == 1, f"{location} was dropped by the scanner"
    assert jobs[0].location == location


@pytest.mark.parametrize("location", OUT_OF_BAY)
def test_lever_keeps_out_of_bay(monkeypatch, location):
    s = LeverScanner(CONFIG)
    payload = [{
        "text": "Business Intelligence Analyst",
        "categories": {"location": location},
        "hostedUrl": "https://jobs.lever.co/acme/1",
        "descriptionPlain": "BI role.",
        "id": "abc123",
    }]
    monkeypatch.setattr(s, "_safe_request", lambda *a, **k: _fake_response(payload))

    jobs = s._scan_company({"name": "Acme", "api_url": "https://api.lever.co/v0/postings/acme"})
    assert len(jobs) == 1, f"{location} was dropped by the scanner"
    assert jobs[0].location == location


@pytest.mark.parametrize("location", OUT_OF_BAY)
def test_ashby_keeps_out_of_bay(monkeypatch, location):
    s = AshbyScanner(CONFIG)
    payload = {"jobs": [{
        "title": "Analytics Engineer",
        "location": location,
        "jobUrl": "https://jobs.ashbyhq.com/acme/1",
        "descriptionPlain": "Analytics role.",
    }]}
    monkeypatch.setattr(s, "_safe_request", lambda *a, **k: _fake_response(payload))

    jobs = s._scan_company({"name": "Acme", "ashby_slug": "acme"})
    assert len(jobs) == 1, f"{location} was dropped by the scanner"


def test_lever_parses_json_not_html(monkeypatch):
    """Regression: LeverScanner used to BeautifulSoup the JSON API and find nothing."""
    s = LeverScanner(CONFIG)
    payload = [
        {"text": "Data Analyst", "categories": {"location": "Austin, TX"},
         "hostedUrl": "https://jobs.lever.co/acme/1", "descriptionPlain": "d", "id": "1"},
        {"text": "Chef", "categories": {"location": "Austin, TX"},
         "hostedUrl": "https://jobs.lever.co/acme/2", "descriptionPlain": "d", "id": "2"},
    ]
    captured = {}

    def fake_request(url, *a, **k):
        captured["url"] = url
        return _fake_response(payload)

    monkeypatch.setattr(s, "_safe_request", fake_request)
    jobs = s._scan_company({"name": "Acme", "api_url": "https://jobs.lever.co/acme"})

    # HTML careers URL must be normalised to the JSON API endpoint.
    assert "api.lever.co/v0/postings/acme" in captured["url"]
    # Relevance filter still applies — "Chef" is not a data role.
    assert [j.title for j in jobs] == ["Data Analyst"]


def test_no_bay_area_allowlist_left_in_ats_scanners():
    """edjoin.py keeps its Bay list on purpose (BT track); the ATS scanners must not."""
    base = Path(__file__).parent.parent / "agents" / "scanner"
    for fname in ("career_pages.py", "ashby.py"):
        src = (base / fname).read_text(encoding="utf-8").lower()
        assert "bay_area_keywords" not in src, f"Bay Area allow-list reintroduced in {fname}"
