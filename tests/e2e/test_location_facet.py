"""Location facet on the home page (added 2026-08-03 for the relocation pivot).

Before this the only geographic control was the free-text "where" box, which
meant browsing by region required knowing what to type. The facet buckets every
posting into regions instead.

Read-only against the live dashboard — no status mutations.
"""

import re

import pytest

pytestmark = pytest.mark.live

TEXAS = re.compile(r"austin|dallas|houston|san antonio|fort worth|plano|irving|texas", re.I)
ARIZONA = re.compile(r"phoenix|tempe|scottsdale|tucson|chandler|arizona", re.I)


def _count(page):
    return page.locator(".card").count()


def _locations(page):
    return page.eval_on_selector_all(".card .loc", "els => els.map(e => e.textContent.trim())")


def _check(page, region):
    page.click(f"#f-locations-{region}")
    page.wait_for_timeout(250)


def test_facet_renders_with_regions(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)

    labels = page.eval_on_selector_all(
        "#rail .facet",
        """els => { const f = els.find(e => (e.querySelector('h4')||{}).textContent === 'Location');
                    return f ? [...f.querySelectorAll('label')].map(l => l.textContent.trim()) : []; }""",
    )
    assert labels, "Location facet did not render"
    # Remote and the Bay must always be offered; the rest depend on current data.
    assert "Remote (US)" in labels
    assert "Bay Area" in labels


def test_selecting_a_region_filters_to_it(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    total = _count(page)

    if not page.locator("#f-locations-texas").count():
        pytest.skip("no Texas jobs in the current dataset")

    _check(page, "texas")
    filtered = _count(page)
    assert 0 < filtered <= total
    for loc in _locations(page):
        assert TEXAS.search(loc), f"non-Texas job survived the Texas filter: {loc}"


def test_multi_location_posting_is_not_hidden(page, base_url):
    """A job listing several cities must appear under EACH of their regions.

    This is the whole reason regionsOf() returns a Set — "Austin, TX; Chicago,
    IL; San Francisco, CA" is a real Texas job and must not be bucketed solely
    as Bay Area.
    """
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)

    if not page.locator("#f-locations-texas").count():
        pytest.skip("no Texas jobs in the current dataset")

    _check(page, "texas")
    multi = [l for l in _locations(page) if ";" in l or "|" in l]
    if not multi:
        pytest.skip("no multi-location Texas postings currently scanned")
    assert all(TEXAS.search(l) for l in multi)


def test_regions_union_and_clear_all(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    total = _count(page)

    if not (page.locator("#f-locations-texas").count() and page.locator("#f-locations-arizona").count()):
        pytest.skip("need both Texas and Arizona jobs for the union check")

    _check(page, "texas")
    tx = _count(page)
    _check(page, "arizona")
    both = _count(page)

    assert both >= tx, "adding a second region must widen, not narrow, the results"
    for loc in _locations(page):
        assert TEXAS.search(loc) or ARIZONA.search(loc), f"unrelated job in union: {loc}"

    page.click("#clearBtn")
    page.wait_for_timeout(250)
    assert _count(page) == total
    assert page.locator('#rail input[data-facet="locations"]:checked').count() == 0
