"""Applied tab (JobRight-style): applied-track jobs leave Home and live under Applied."""

import pytest

pytestmark = pytest.mark.live

APPLIED_TRACK = ["applied", "response_received", "interview", "rejected", "no_response"]


def _load(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    # `state` is a top-level const (not on window) — probe it via typeof
    page.wait_for_function(
        "typeof state !== 'undefined' && state.apps && state.apps.length > 0", timeout=15000
    )


def test_nav_has_applied_link(page, base_url):
    _load(page, base_url)
    link = page.locator('.topnav a[data-view="applied"]')
    assert link.count() == 1
    assert "applied" in link.inner_text().lower()


def test_home_excludes_applied_track_jobs(page, base_url):
    _load(page, base_url)
    page.wait_for_selector(".card", timeout=15000)
    shown = page.evaluate(
        "[...document.querySelectorAll('#results .card')].map(c => +c.dataset.id)"
    )
    applied_ids = page.evaluate(
        f"state.apps.filter(a => {APPLIED_TRACK!r}.includes(a.status)).map(a => a.id)"
    )
    leaked = set(shown) & set(applied_ids)
    assert not leaked, f"applied-track jobs leaked onto Home: {sorted(leaked)}"


def test_applied_view_shows_applied_jobs(page, base_url):
    _load(page, base_url)
    page.click('.topnav a[data-view="applied"]')
    assert not page.locator("#appliedView").is_hidden()
    # Home feed is hidden while the Applied view is open
    assert page.locator("main").evaluate("el => el.style.display") == "none"
    expected = page.evaluate(
        f"state.apps.filter(a => {APPLIED_TRACK!r}.includes(a.status)).length"
    )
    rows = page.locator("#appliedList .ap-row").count()
    assert rows == expected, f"expected {expected} applied rows, saw {rows}"
    # Status sub-tabs render with counts
    assert page.locator("#apTabs .ap-tab").count() == 6


def test_applied_tab_filters_by_status(page, base_url):
    _load(page, base_url)
    page.click('.topnav a[data-view="applied"]')
    for status in APPLIED_TRACK:
        page.click(f'#apTabs .ap-tab[data-tab="{status}"]')
        expected = page.evaluate(f"state.apps.filter(a => a.status === {status!r}).length")
        rows = page.locator("#appliedList .ap-row").count()
        assert rows == expected, f"tab {status}: expected {expected} rows, saw {rows}"
    page.click('#apTabs .ap-tab[data-tab="all"]')


def test_applied_row_opens_detail_on_home(page, base_url):
    _load(page, base_url)
    page.click('.topnav a[data-view="applied"]')
    rows = page.locator("#appliedList .ap-row")
    if rows.count() == 0:
        pytest.skip("no applied jobs in this database")
    rows.first.click()
    # Clicking a row jumps to Home with the job's detail open
    assert page.locator("#appliedView").is_hidden()
    # h3 headings are CSS-uppercased and innerText reflects text-transform
    assert "job details" in page.locator("#detail").inner_text().lower()


def test_no_console_errors_on_applied_view(page, base_url):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    _load(page, base_url)
    page.click('.topnav a[data-view="applied"]')
    page.wait_for_timeout(1500)
    real = [e for e in errors if "favicon" not in e.lower()]
    assert not real, f"console errors on Applied view: {real}"
