"""Free LinkedIn sources: parser, block handling and the optional-dep contract.

NOTHING HERE TOUCHES THE NETWORK. Every test patches the provider's requests
Session, because the whole point of the module under test is a source that
LinkedIn blocks on volume — a test suite that hit the real endpoint would be
the exact behaviour the module exists to avoid.

The HTML below is a trimmed copy of a real
``/jobs-guest/jobs/api/seeMoreJobPostings/search`` response captured on
2026-09-09 (class names, the ``urn:li:jobPosting:`` entity urn, the tracking
query string on the card href and the bare ``<li>`` sequence with no document
wrapper are all verbatim). If LinkedIn's markup changes, this fixture is the
record of what it used to be and the parser is what has to move.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.scanner.base import RawJob
from agents.scanner.linkedin_sources import (
    LINKEDIN_PROVIDERS,
    JobSpyProvider,
    LinkedInPublicProvider,
    _canonical_job_url,
    _num,
)


# --------------------------------------------------------------------------
# fixtures: recorded markup
# --------------------------------------------------------------------------

def _card(job_id: str, title: str, company_html: str, location_html: str,
          time_html: str = '<time class="job-search-card__listdate" datetime="2026-09-01">1 week ago</time>',
          extra: str = "") -> str:
    return f"""
      <li>
      <div class="base-card relative w-full base-card--link base-search-card
        base-search-card--link job-search-card"
        data-entity-urn="urn:li:jobPosting:{job_id}"
        data-impression-id="jobs-search-result-0">
        <a class="base-card__full-link absolute top-0 right-0"
           href="https://www.linkedin.com/jobs/view/{title.lower().replace(' ', '-')}-at-x-{job_id}?position=1&amp;pageNum=0&amp;refId=JUx7vLoImMcRDDTEzENS3g%3D%3D&amp;trackingId=jQX2H0VW3fF30gPn5U1Czg%3D%3D">
          <span class="sr-only">{title}</span>
        </a>
        <div class="base-search-card__info">
          <h3 class="base-search-card__title">{title}</h3>
          {company_html}
          <div class="base-search-card__metadata">
            {location_html}
            {time_html}
          </div>
          {extra}
        </div>
      </div>
      </li>
    """


def _subtitle(company: str) -> str:
    return (f'<h4 class="base-search-card__subtitle">'
            f'<a class="hidden-nested-link" href="https://www.linkedin.com/company/x">'
            f'{company}</a></h4>')


def _location(loc: str) -> str:
    return f'<span class="job-search-card__location">{loc}</span>'


FRAGMENT_TWO_CARDS = (
    _card("4462044323", "Data Analyst", _subtitle("High Trail"), _location("United States"))
    + _card("4439105297", "Senior Data Engineer", _subtitle("Acme Corp"),
            _location("Austin, TX"),
            time_html='<time class="job-search-card__listdate--new" datetime="2026-08-31">2 days ago</time>',
            extra='<span class="job-search-card__salary-info">$120,000 - $150,000</span>')
)

# A card with no <h4> subtitle, no <span class=location>, no <time>. Real cards
# do occasionally ship without a company link or a listdate.
FRAGMENT_PARTIAL = _card("4400000001", "Analytics Lead", "", "", time_html="")

# Card with no title text at all and no entity urn — must be dropped, not crash.
FRAGMENT_TITLELESS = """
  <li>
  <div class="base-card base-search-card job-search-card">
    <a class="base-card__full-link" href="https://www.linkedin.com/jobs/view/x-1?refId=z"></a>
    <div class="base-search-card__info"><h3 class="base-search-card__title"></h3></div>
  </div>
  </li>
"""

DETAIL_HTML = """
<div class="description__text description__text--rich">
  <section class="show-more-less-html" data-max-lines="5">
    <div class="show-more-less-html__markup show-more-less-html__markup--clamp-after-5">
      <p><strong>Job Description</strong></p><p>We want dbt and SQL. Fully remote role.</p>
    </div>
  </section>
</div>
<ul class="description__job-criteria-list">
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Seniority level</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Mid-Senior level</span>
  </li>
  <li class="description__job-criteria-item">
    <h3 class="description__job-criteria-subheader">Employment type</h3>
    <span class="description__job-criteria-text description__job-criteria-text--criteria">Contract</span>
  </li>
</ul>
"""


# --------------------------------------------------------------------------
# helpers: a fake session that never touches the network
# --------------------------------------------------------------------------

class FakeSession:
    """Records calls and replays a queued list of (status, text) responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []
        self.headers: dict = {}

    def get(self, url, params=None, timeout=None, **kw):
        self.calls.append((url, dict(params or {})))
        if self._responses:
            status, text = self._responses.pop(0)
        else:
            status, text = 400, ""          # measured end-of-results signal
        if isinstance(status, Exception):
            raise status
        return SimpleNamespace(status_code=status, text=text)


def _provider(responses, **config) -> LinkedInPublicProvider:
    cfg = {"delay_seconds": 0.01, "max_requests": 25}
    cfg.update(config)
    p = LinkedInPublicProvider(cfg)
    p.session = FakeSession(responses)
    return p


# --------------------------------------------------------------------------
# card parsing
# --------------------------------------------------------------------------

def test_parses_recorded_fragment_into_rawjobs():
    p = _provider([])
    jobs = p._parse_cards(FRAGMENT_TWO_CARDS)

    assert len(jobs) == 2
    assert all(isinstance(j, RawJob) for j in jobs)

    a, b = jobs
    assert a.title == "Data Analyst"
    assert a.company == "High Trail"
    assert a.location == "United States"
    assert a.source == "linkedin_public"
    assert a.source_id == "4462044323"
    assert a.date_posted is not None and a.date_posted.year == 2026
    assert a.date_posted.month == 9 and a.date_posted.day == 1

    assert b.title == "Senior Data Engineer"
    assert b.company == "Acme Corp"
    assert b.location == "Austin, TX"
    assert b.salary_text == "$120,000 - $150,000"
    assert b.source_id == "4439105297"


def test_canonical_url_strips_per_impression_tracking():
    """refId/trackingId are unique per response and would defeat URL dedup."""
    p = _provider([])
    job = p._parse_cards(FRAGMENT_TWO_CARDS)[0]
    assert job.url == "https://www.linkedin.com/jobs/view/data-analyst-at-x-4462044323"
    assert "?" not in job.url
    assert "trackingId" not in job.url
    assert job.url.startswith("https://www.linkedin.com/jobs/view/")


def test_canonical_url_falls_back_to_job_id():
    assert _canonical_job_url("", "123") == "https://www.linkedin.com/jobs/view/123"
    assert _canonical_job_url("", "") == ""
    assert _canonical_job_url("not a url", "77") == "https://www.linkedin.com/jobs/view/77"


def test_missing_fields_do_not_drop_a_card_with_title_and_url():
    p = _provider([])
    jobs = p._parse_cards(FRAGMENT_PARTIAL)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Analytics Lead"
    assert j.company == ""          # no <h4> at all
    assert j.location == ""
    assert j.date_posted is None    # no <time datetime>
    assert j.salary_text == ""
    assert j.url.endswith("4400000001")


def test_card_without_a_title_is_skipped():
    p = _provider([])
    assert p._parse_cards(FRAGMENT_TITLELESS) == []


def test_empty_and_whitespace_response_parses_to_nothing():
    p = _provider([])
    assert p._parse_cards("") == []
    assert p._parse_cards("   \n  ") == []
    assert p._parse_cards("<li></li>") == []


def test_remote_flag_inferred_from_location_and_title():
    p = _provider([])
    html = _card("1", "Data Analyst", _subtitle("X"), _location("Remote, United States"))
    assert p._parse_cards(html)[0].is_remote is True
    html = _card("2", "Data Analyst", _subtitle("X"), _location("Austin, TX"))
    assert p._parse_cards(html)[0].is_remote is False


# --------------------------------------------------------------------------
# search(): paging, caps, delays
# --------------------------------------------------------------------------

def test_search_returns_jobs_and_stops_on_end_of_results():
    """Measured: `start` past the end returns HTTP 400 with an empty body."""
    p = _provider([(200, FRAGMENT_TWO_CARDS), (400, "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("data analyst", "United States", 50)
    assert [j.title for j in jobs] == ["Data Analyst", "Senior Data Engineer"]
    assert len(p.session.calls) == 2
    assert p.session.calls[0][1]["start"] == 0
    assert p.session.calls[0][1]["keywords"] == "data analyst"
    assert p.session.calls[1][1]["start"] == 10


def test_search_honours_limit_and_dedups_by_url():
    p = _provider([(200, FRAGMENT_TWO_CARDS), (200, FRAGMENT_TWO_CARDS), (400, "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 1)
    assert len(jobs) == 1
    assert len(p.session.calls) == 1          # limit reached, no second page


def test_rate_limit_delay_is_invoked_between_requests():
    p = _provider([(200, FRAGMENT_TWO_CARDS), (200, FRAGMENT_TWO_CARDS), (400, "")],
                  delay_seconds=3.0)
    with patch("agents.scanner.linkedin_sources.time.sleep") as sleep:
        p.search("x", "", 50)
    # 3 requests -> a pause before every one after the first.
    assert sleep.call_count == 2
    assert all(c.args[0] == 3.0 for c in sleep.call_args_list)


def test_delay_floor_cannot_be_configured_below_two_seconds():
    assert LinkedInPublicProvider({"delay_seconds": 0.0}).delay_seconds == 2.0
    assert LinkedInPublicProvider({}).delay_seconds == 2.5
    assert LinkedInPublicProvider({"delay_seconds": 9}).delay_seconds == 9.0


def test_per_run_request_cap_stops_the_run():
    pages = [(200, FRAGMENT_TWO_CARDS)] * 10
    p = _provider(pages, max_requests=3)
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        p.search("x", "", 500)
    assert len(p.session.calls) == 3
    assert "cap reached" in p._block_reason


# --------------------------------------------------------------------------
# block handling
# --------------------------------------------------------------------------

def test_http_999_aborts_immediately_without_retry():
    p = _provider([(999, ""), (200, FRAGMENT_TWO_CARDS)])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 50)
    assert jobs == []
    assert len(p.session.calls) == 1            # no retry on a 999
    assert p._blocked is True
    h = p.health()
    assert h["ready"] is False
    assert "999" in h["reason"]


def test_http_429_gets_exactly_one_backed_off_retry_then_succeeds():
    p = _provider([(429, ""), (200, FRAGMENT_TWO_CARDS), (400, "")], delay_seconds=2.0)
    with patch("agents.scanner.linkedin_sources.time.sleep") as sleep:
        jobs = p.search("x", "", 50)
    assert len(jobs) == 2
    assert len(p.session.calls) == 3            # 429, retry(ok), end-of-results
    assert 8.0 in [c.args[0] for c in sleep.call_args_list]   # backoff = delay * 4


def test_http_429_twice_gives_up_for_the_cycle():
    p = _provider([(429, ""), (429, ""), (200, FRAGMENT_TWO_CARDS)])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 50)
    assert jobs == []
    assert len(p.session.calls) == 2
    assert p._blocked is True
    assert "throttled" in p.health()["reason"]


def test_http_403_aborts_and_reports_reason():
    p = _provider([(403, "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        assert p.search("x", "", 50) == []
    assert p.health()["ready"] is False
    assert "403" in p.health()["reason"]


def test_503_is_retried_like_429():
    p = _provider([(503, ""), (200, FRAGMENT_TWO_CARDS), (400, "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        assert len(p.search("x", "", 50)) == 2


def test_transport_exception_degrades_to_empty_list():
    p = _provider([(ConnectionError("dns down"), "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        assert p.search("x", "", 50) == []


def test_health_on_a_fresh_instance_is_ready_with_the_caveat():
    h = LinkedInPublicProvider({}).health()
    assert h["ready"] is True
    assert h["missing_keys"] == []
    assert h["name"] == "linkedin_public"
    assert h["label"]
    assert "unofficial" in h["reason"]
    assert h["attribution"]


# --------------------------------------------------------------------------
# description fetching (opt-in)
# --------------------------------------------------------------------------

def test_descriptions_are_not_fetched_unless_the_flag_is_on():
    p = _provider([(200, FRAGMENT_TWO_CARDS), (400, "")])
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 50)
    assert all(j.description == "" for j in jobs)
    assert all("jobPosting" not in url for url, _ in p.session.calls)


def test_fetch_descriptions_follows_each_card_and_fills_body_and_seniority():
    p = _provider([(200, FRAGMENT_PARTIAL), (400, ""), (200, DETAIL_HTML)],
                  fetch_descriptions=True)
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 50)
    assert len(jobs) == 1
    j = jobs[0]
    assert "dbt and SQL" in j.description
    assert "<p>" not in j.description            # html stripped
    assert j.seniority_level == "Mid-Senior level"
    assert j.is_remote is True                   # "Fully remote role." in the body
    assert p.session.calls[-1][0].endswith("/jobPosting/4400000001")


def test_a_failed_detail_fetch_keeps_the_job():
    p = _provider([(200, FRAGMENT_PARTIAL), (400, ""), (500, "")], fetch_descriptions=True)
    with patch("agents.scanner.linkedin_sources.time.sleep"):
        jobs = p.search("x", "", 50)
    assert len(jobs) == 1
    assert jobs[0].description == ""


# --------------------------------------------------------------------------
# JobSpyProvider — optional dependency
# --------------------------------------------------------------------------

def test_jobspy_is_not_installed_here():
    """Guard for the rest of this section: the adapter must stay optional."""
    with pytest.raises(ImportError):
        import jobspy  # noqa: F401


def test_jobspy_health_reports_the_install_hint_when_absent():
    h = JobSpyProvider({}).health()
    assert h["ready"] is False
    assert h["reason"] == "pip install python-jobspy"
    assert h["name"] == "jobspy"
    assert h["missing_keys"] == []          # optional dep, not a missing key


def test_jobspy_search_returns_empty_when_the_lazy_import_fails():
    p = JobSpyProvider({})
    assert p.search("data analyst", "Austin, TX", 10) == []


def test_jobspy_import_is_lazy_not_at_module_import_time():
    """Importing the module must not require python-jobspy."""
    import importlib

    mod = importlib.import_module("agents.scanner.linkedin_sources")
    assert mod.JobSpyProvider._load() is None       # absent, but nothing raised


def test_jobspy_maps_rows_to_rawjob_when_the_package_is_present():
    rows = [{
        "site": "linkedin",
        "id": "li-1",
        "title": "Data Analyst",
        "company": "Acme",
        "location": "Austin, TX",
        "job_url": "https://www.linkedin.com/jobs/view/999",
        "description": "<p>SQL &amp; dbt</p>",
        "date_posted": "2026-09-01",
        "is_remote": False,
        "min_amount": 100000.0,
        "max_amount": 130000.0,
        "interval": "yearly",
        "job_level": "mid-senior level",
    }]
    p = JobSpyProvider({})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: (lambda **kw: rows))):
        jobs = p.search("data analyst", "Austin, TX", 10)
    assert len(jobs) == 1
    j = jobs[0]
    assert j.title == "Data Analyst"
    assert j.company == "Acme"
    assert j.url == "https://www.linkedin.com/jobs/view/999"
    assert j.source == "jobspy_linkedin"
    assert j.description == "SQL & dbt"
    assert j.salary_min == 100000.0 and j.salary_max == 130000.0
    assert j.salary_text == "yearly"
    assert j.source_id == "li-1"
    assert j.date_posted is not None and j.date_posted.day == 1
    assert j.seniority_level == "mid-senior level"


def test_jobspy_drops_rows_without_a_title_or_url_and_honours_limit():
    rows = [
        {"title": "", "job_url": "https://x/1"},
        {"title": "Has no url", "job_url": ""},
        {"title": "Keep me", "job_url": "https://x/3", "site": "indeed"},
        {"title": "Over limit", "job_url": "https://x/4", "site": "indeed"},
        "not a dict",
    ]
    p = JobSpyProvider({})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: (lambda **kw: rows))):
        jobs = p.search("x", "", 1)
    assert [j.title for j in jobs] == ["Keep me"]


def test_jobspy_scrape_failure_is_contained():
    def boom(**kw):
        raise RuntimeError("linkedin blocked jobspy")

    p = JobSpyProvider({})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: boom)):
        assert p.search("x", "", 10) == []


def test_jobspy_normalises_a_dataframe_like_result_without_pandas():
    """pandas is not installed; the adapter must duck-type, never import it."""
    class FakeFrame:
        def to_dict(self, orient):
            assert orient == "records"
            return [{"title": "T", "job_url": "https://x/1", "site": "glassdoor"}]

    p = JobSpyProvider({})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: (lambda **kw: FakeFrame()))):
        jobs = p.search("x", "", 10)
    assert len(jobs) == 1 and jobs[0].source == "jobspy_glassdoor"


def test_pandas_nan_salaries_never_reach_rawjob():
    nan = float("nan")
    assert _num(nan) is None
    assert _num(None) is None
    assert _num("") is None
    assert _num("120000") == 120000.0
    rows = [{"title": "T", "job_url": "https://x/1", "min_amount": nan, "max_amount": nan,
             "date_posted": nan}]
    p = JobSpyProvider({})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: (lambda **kw: rows))):
        j = p.search("x", "", 10)[0]
    assert j.salary_min is None and j.salary_max is None and j.date_posted is None


def test_jobspy_passes_expected_kwargs_through():
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return []

    p = JobSpyProvider({"sites": ["linkedin"], "hours_old": 72, "country_indeed": "USA",
                        "fetch_descriptions": True})
    with patch.object(JobSpyProvider, "_load", staticmethod(lambda: fake)):
        p.search("data analyst", "Austin, TX", 25)
    assert seen["site_name"] == ["linkedin"]
    assert seen["search_term"] == "data analyst"
    assert seen["location"] == "Austin, TX"
    assert seen["results_wanted"] == 25
    assert seen["hours_old"] == 72
    assert seen["country_indeed"] == "USA"
    assert seen["linkedin_fetch_description"] is True


# --------------------------------------------------------------------------
# registry contract
# --------------------------------------------------------------------------

def test_registry_exports_both_providers_with_the_provider_health_contract():
    assert set(LINKEDIN_PROVIDERS) == {"linkedin_public", "jobspy"}
    for name, cls in LINKEDIN_PROVIDERS.items():
        assert cls.name == name
        h = cls({}).health()
        assert set(h) >= {"name", "label", "ready", "missing_keys", "attribution", "reason"}
        assert isinstance(h["ready"], bool)
        assert isinstance(h["missing_keys"], list)


def test_registry_does_not_collide_with_the_existing_providers():
    """A LinkedIn provider must never SHADOW a built-in one.

    This used to assert the two name sets never intersect, which held only
    while the merge had not happened yet. providers._ensure_linkedin_providers
    now merges them on first use (lazily, to avoid an import cycle), so the
    intersection is expected and the real invariant is that the merge ADDS
    names rather than replacing anyone else's class.
    """
    from agents.scanner import providers as P

    builtin = {"jsearch", "adzuna", "jooble", "jobicy",
               "arbeitnow", "remotive", "usajobs"}
    assert not (builtin & set(LINKEDIN_PROVIDERS)), "a LinkedIn name shadows a built-in"

    P._ensure_linkedin_providers()
    for name, cls in LINKEDIN_PROVIDERS.items():
        assert P.PROVIDERS[name] is cls
    assert builtin <= set(P.PROVIDERS), "the merge dropped a built-in provider"


def test_lazy_registration_is_idempotent_and_order_independent():
    """Registering twice must not duplicate or drop anything.

    The merge is lazy because linkedin_sources imports JobProvider from
    providers; an import-time merge is a cycle that silently half-works
    depending on which module is imported first.
    """
    from agents.scanner import providers as P

    P._ensure_linkedin_providers()
    once = dict(P.PROVIDERS)
    P._ensure_linkedin_providers()
    assert dict(P.PROVIDERS) == once


def test_neither_provider_requires_an_api_key():
    for cls in LINKEDIN_PROVIDERS.values():
        assert cls.requires_key == ()
        assert cls({}).missing_keys() == []
