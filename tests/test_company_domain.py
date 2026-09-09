"""Layered domain discovery — trust order, and the board-host rule.

Domain failure, not mailbox failure, is what stops the mail agent: of 25 misses
in a 40-job sample, 16 were `domain_unconfirmed`. This module exists to raise
that hit rate using evidence already in the repo, without ever claiming more
confidence than the source justifies.

The two rules that must never regress:
  * a job board or ATS host is NEVER the employer's domain — missing that once
    made every company resolve to careers@linkedin.com;
  * `authoritative=True` is claimed only for a curated source or the employer's
    own posting host, because it lets the caller SKIP the ownership check.

Offline: no test may perform a DNS or HTTP lookup.
"""

from __future__ import annotations

import pytest

import utils.company_domain as D


class _Job:
    def __init__(self, company="Acme Robotics", url="", extra_urls=None):
        self.company = company
        self.url = url
        self.extra_urls = extra_urls


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """MX lookups are stubbed; every candidate domain 'has MX' unless stated."""
    monkeypatch.setattr(D, "has_mx", lambda d: True)
    clear = getattr(D.load_target_domains, "cache_clear", None)
    if clear:
        clear()
    yield
    clear = getattr(D.load_target_domains, "cache_clear", None)
    if clear:
        clear()


# ------------------------------------------------------------------ helpers --

@pytest.mark.parametrize("domain,label", [
    ("www.acme.co.uk", "acme"), ("acme.com", "acme"),
    ("boards.greenhouse.io", "boards"), ("", ""),
])
def test_domain_label(domain, label):
    assert D.domain_label(domain) == label


@pytest.mark.parametrize("host", [
    "linkedin.com", "www.linkedin.com", "es.linkedin.com", "indeed.com",
    "glassdoor.com", "boards.greenhouse.io", "jobs.lever.co",
    "acme.myworkdayjobs.com", "jobs.ashbyhq.com",
])
def test_board_and_ats_hosts_are_never_employer_domains(host):
    assert D.is_board_or_ats(host) is True


@pytest.mark.parametrize("host", ["acme.com", "anthropic.com", "lbl.gov"])
def test_real_employer_hosts_are_not_flagged(host):
    assert D.is_board_or_ats(host) is False


@pytest.mark.parametrize("domain,expected", [
    ("kaiserpermanentejobs.org", True),
    ("acmecareers.com", True),
    ("acme.com", False),
    ("jobs.com", False),          # the label IS the word — not a company careers site
])
def test_careers_domain_detection(domain, expected):
    assert D.is_careers_domain(domain) is expected


# ------------------------------------------------------------- the ATS slug --

@pytest.mark.parametrize("url,slug,ats", [
    ("https://job-boards.greenhouse.io/anthropic/jobs/5390966008", "anthropic", "greenhouse.io"),
    ("https://boards.greenhouse.io/acme/jobs/123", "acme", "greenhouse.io"),
    ("https://jobs.lever.co/ramp/abc-123", "ramp", "lever.co"),
    ("https://jobs.ashbyhq.com/vanta/xyz", "vanta", "ashbyhq.com"),
    ("https://acme.wd1.myworkdayjobs.com/en-US/careers/job/x", "acme", "myworkdayjobs.com"),
])
def test_ats_url_names_the_employer_not_the_ats(url, slug, ats):
    got = D.domain_from_ats_url(url)
    assert got is not None, url
    assert got[0] == slug and ats in got[1]


@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-123",
    "https://www.indeed.com/viewjob?jk=abc",
    "https://acme.com/careers/engineer",
    "",
])
def test_non_ats_urls_yield_no_slug(url):
    assert D.domain_from_ats_url(url) is None


def test_extra_urls_source_field_is_read():
    """{"source": "greenhouse:anthropic"} states the slug outright."""
    got = D._slugs_from_extra_urls(
        [{"source": "greenhouse:anthropic",
          "url": "https://job-boards.greenhouse.io/anthropic/jobs/1"}])
    assert ("anthropic", "greenhouse") in got


def test_extra_urls_accepts_a_json_string():
    got = D._slugs_from_extra_urls('[{"source": "lever:ramp", "url": ""}]')
    assert ("ramp", "lever") in got


def test_extra_urls_survives_garbage():
    assert D._slugs_from_extra_urls("not json") == []
    assert D._slugs_from_extra_urls(None) == []
    assert D._slugs_from_extra_urls([1, "x", None]) == []


# --------------------------------------------------------------- trust order --

def test_posting_host_beats_a_name_guess():
    job = _Job("Acme Robotics", "https://acmerobotics.com/careers/eng")
    r = D.resolve_domain(job)
    assert r["domain"] == "acmerobotics.com"
    assert r["origin"] == "posting_host"
    assert r["authoritative"] is True


def test_curated_yaml_beats_the_posting_host(monkeypatch):
    monkeypatch.setattr(D, "load_target_domains", lambda: {"acme robotics": "acme.com"})
    job = _Job("Acme Robotics", "https://acmerobotics.io/careers/eng")
    r = D.resolve_domain(job)
    assert r["domain"] == "acme.com"
    assert r["origin"] == "target_yaml"
    assert r["authoritative"] is True


def test_ats_slug_is_used_when_the_posting_is_on_a_board(monkeypatch):
    # An employer absent from the curated file, so the slug layer is what answers.
    monkeypatch.setattr(D, "load_target_domains", lambda: {})
    job = _Job("Wibble Systems", "https://www.linkedin.com/jobs/view/x-at-wibble-1",
               extra_urls=[{"source": "greenhouse:wibblesystems", "url": ""}])
    r = D.resolve_domain(job)
    assert r["domain"] == "wibblesystems.com"
    assert r["origin"] == "ats_slug"
    assert r["authoritative"] is False, "an inferred slug still has to prove ownership"


def test_a_board_url_alone_falls_through_to_the_name_guess():
    job = _Job("Acme Robotics", "https://www.linkedin.com/jobs/view/eng-at-acme-1")
    r = D.resolve_domain(job)
    assert r["origin"] == "name_guess"
    assert r["authoritative"] is False


def test_extra_urls_supplies_a_corporate_host():
    job = _Job("Acme", "https://www.linkedin.com/jobs/view/x-1",
               extra_urls=[{"source": "site", "url": "https://acme.com/jobs/1"}])
    r = D.resolve_domain(job)
    assert r["domain"] == "acme.com"
    assert r["origin"] == "extra_urls"


# ------------------------------------------------------- the board-host rule --

@pytest.mark.parametrize("url", [
    "https://www.linkedin.com/jobs/view/ai-engineer-at-acme-4464323868",
    "https://es.linkedin.com/jobs/view/x",
    "https://www.indeed.com/viewjob?jk=abc123",
    "https://www.glassdoor.com/job-listing/x",
])
def test_a_board_host_is_never_returned_as_the_domain(url):
    r = D.resolve_domain(_Job("Acme", url))
    assert r["domain"] is None or "linkedin" not in r["domain"]
    assert r["domain"] is None or "indeed" not in r["domain"]
    assert r["domain"] is None or "glassdoor" not in r["domain"]


def test_an_ats_host_is_never_returned_as_the_domain():
    r = D.resolve_domain(_Job("Acme", "https://boards.greenhouse.io/acme/jobs/1"))
    assert r["domain"] is None or "greenhouse" not in r["domain"]


def test_every_candidate_is_filtered_not_just_the_winner():
    job = _Job("Acme", "https://www.linkedin.com/jobs/view/x-1",
               extra_urls=[{"source": "s", "url": "https://www.indeed.com/viewjob?jk=1"}])
    r = D.resolve_domain(job)
    for c in r["candidates"]:
        assert not D.is_board_or_ats(c["domain"])


# ------------------------------------------------------------------- shape ----

def test_shape_is_stable_when_nothing_resolves():
    r = D.resolve_domain(_Job("", ""))
    assert set(r) == {"domain", "origin", "authoritative", "is_careers_domain",
                      "candidates", "reason"}
    assert r["domain"] is None and r["reason"]


def test_candidates_are_in_trust_order():
    job = _Job("Acme", "https://acme.com/jobs/1",
               extra_urls=[{"source": "greenhouse:acmecorp", "url": ""}])
    r = D.resolve_domain(job)
    idx = [D.ORIGINS.index(c["origin"]) for c in r["candidates"]]
    assert idx == sorted(idx)


def test_a_careers_domain_is_flagged(monkeypatch):
    monkeypatch.setattr(D, "load_target_domains",
                        lambda: {"kaiser permanente": "kaiserpermanentejobs.org"})
    r = D.resolve_domain(_Job("Kaiser Permanente", ""))
    assert r["domain"] == "kaiserpermanentejobs.org"
    assert r["is_careers_domain"] is True, "mail does not land on a careers domain"


def test_resolve_never_raises_on_a_malformed_job():
    class Bad:
        company = "Acme"
        url = "http://[not-a-url"
        extra_urls = {"unexpected": "shape"}
    assert D.resolve_domain(Bad())["domain"] in (None, "acme.com")


def test_no_mx_means_no_name_guess(monkeypatch):
    monkeypatch.setattr(D, "has_mx", lambda d: False)
    r = D.resolve_domain(_Job("Nonexistent Widgets", ""))
    assert r["domain"] is None


def test_offline_mode_skips_dns(monkeypatch):
    def _boom(d):                      # pragma: no cover - guard
        raise AssertionError("allow_network=False must not do DNS")
    monkeypatch.setattr(D, "has_mx", _boom)
    r = D.resolve_domain(_Job("Acme Robotics", ""), allow_network=False)
    assert r["domain"] == "acmerobotics.com"
