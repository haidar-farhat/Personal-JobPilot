"""Render smoke tests for the Indeed-style JobPilot dashboard (Playwright, live)."""

import pytest

pytestmark = pytest.mark.live


def test_page_title(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    assert "JobPilot" in page.title()


def test_two_field_search_renders(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    assert page.locator("#what").count() == 1
    assert page.locator("#where").count() == 1
    assert page.locator("#findBtn").is_visible()


def test_filter_rail_renders(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    rail = page.locator("#rail")
    assert rail.is_visible()
    # AI-forward toggle + the core facet headings
    assert page.locator("#f-ai").count() == 1
    text = rail.inner_text().lower()
    for heading in ["role category", "fit tier", "job type", "date posted", "source", "status"]:
        assert heading in text, f"missing facet: {heading}"


def test_cards_render(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    assert page.locator(".card").count() > 0


def test_no_console_errors_on_load(page, base_url):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    real = [e for e in errors if "favicon" not in e.lower()]
    assert not real, f"console errors on load: {real}"
