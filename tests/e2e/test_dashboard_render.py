"""Smoke tests for JobPilot dashboard render — Playwright UI."""

import re

import pytest


pytestmark = pytest.mark.live


STAT_IDS = [
    "stat-total",
    "stat-today",
    "stat-applied",
    "stat-interviews",
    "stat-avg-score",
    "stat-response-rate",
    "stat-autoapply",  # renamed from stat-followup when auto-apply landed
    "stat-high",
]


def test_page_loads_with_correct_title(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    title = page.title()
    # The Cromaz AI rebrand keeps "Cromaz AI" as primary brand
    assert "Cromaz AI" in title or "Job Search" in title, f"unexpected title: {title!r}"


def test_all_eight_stat_cards_present(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    for stat_id in STAT_IDS:
        loc = page.locator(f"#{stat_id}")
        assert loc.count() == 1, f"Missing stat card: #{stat_id}"
        assert loc.is_visible(), f"Stat card #{stat_id} is hidden"


def test_stat_values_are_numeric(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)  # let SSE first tick land
    for stat_id in STAT_IDS:
        raw = page.locator(f"#{stat_id}").inner_text()
        # The dashboard renders some stats with an inline <span class="suffix">%</span>
        # which inner_text() exposes with a newline between the digits and the unit.
        normalized = re.sub(r"\s+", "", raw)
        # accept "0", "12", "12.5", "12%", "12.5%"
        assert re.match(r"^[\d.]+%?$", normalized), f"#{stat_id} has non-numeric value: {raw!r}"


def test_filter_pills_render(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    pills = page.locator("button.filter-pill")
    assert pills.count() >= 5, "expected at least 5 filter pills"
    # The "All" pill must be visible and have data-filter="all"
    all_pill = page.locator('button.filter-pill[data-filter="all"]')
    assert all_pill.count() == 1
    assert all_pill.is_visible()


def test_search_input_renders(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    search = page.locator("#search-input")
    assert search.count() == 1
    assert search.is_visible()
    assert search.get_attribute("placeholder") is not None


def test_application_table_present(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    tbody = page.locator("#app-table-body")
    assert tbody.count() == 1


def test_drawer_starts_closed(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    overlay = page.locator("#drawer-overlay")
    assert overlay.count() == 1
    # The drawer-overlay does not have the `.open` class on initial load
    classes = overlay.get_attribute("class") or ""
    assert "open" not in classes.split(), f"drawer should be closed but had: {classes!r}"


def test_no_console_errors_on_load(page, base_url):
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)  # give animations + SSE first tick a chance
    # Allow SSE ping noise but not real exceptions
    fatal = [e for e in errors if "EventSource" not in e and "favicon" not in e.lower()]
    assert not fatal, f"fatal console errors: {fatal}"
