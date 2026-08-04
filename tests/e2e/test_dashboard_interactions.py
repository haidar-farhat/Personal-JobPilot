"""Interactive UI tests for the Indeed-style dashboard — search, facets, detail, AI-forward.

Read-only against the live dashboard (no status mutations, to avoid changing real data).
"""

import pytest

pytestmark = pytest.mark.live


def _count(page):
    return page.locator(".card").count()


def test_what_search_filters_list(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    total = _count(page)
    page.fill("#what", "scientist")
    page.click("#findBtn")
    page.wait_for_timeout(500)
    filtered = _count(page)
    assert 0 < filtered <= total
    # The search haystack is title + company + ats_keywords (LLM-extracted) —
    # verify every visible card matches in one of those fields.
    ids = page.eval_on_selector_all(".card", "els => els.map(e => +e.dataset.id)")
    # Must match the limit index.html fetches with, or cards rendered from the
    # UI's larger result set won't be found here (the cap was raised to 2000 on
    # 2026-08-03 so out-of-metro jobs stop falling off the fit_score-sorted cut).
    apps = page.evaluate("fetch('/api/applications?limit=2000').then(r => r.json())")
    by_id = {a["id"]: a for a in apps}
    for card_id in ids:
        a = by_id.get(card_id)
        assert a is not None, f"card {card_id} not in API response"
        hay = " ".join([
            a.get("title") or "", a.get("company") or "",
            " ".join(a.get("ats_keywords") or []),
        ]).lower()
        assert "scientist" in hay, f"card {card_id} ({a.get('title')}) doesn't match search"


def test_role_category_facet_filters(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    total = _count(page)
    # check the first role-category checkbox
    box = page.locator('#rail input[data-facet="categories"]').first
    box.check()
    page.wait_for_timeout(400)
    assert _count(page) <= total


def test_card_click_opens_detail(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    page.locator(".card").first.click()
    page.wait_for_selector(".d-head h2", timeout=5000)
    assert page.locator(".d-head h2").inner_text().strip() != ""
    # status control + dimension grid present
    assert page.locator("#statusSel").count() == 1


def test_ai_forward_toggle_shows_only_ai(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    page.check("#f-ai")
    page.wait_for_timeout(400)
    n = _count(page)
    if n:
        # every visible card must carry the AI-forward pill
        badges = page.eval_on_selector_all(".card", "els => els.map(e => e.innerText.includes('AI-forward'))")
        assert all(badges), "AI-forward filter showed a non-AI card"


def test_clear_all_restores_list(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    total = _count(page)
    page.locator('#rail input[data-facet="tiers"]').first.check()
    page.wait_for_timeout(300)
    page.click("#clearBtn")
    page.wait_for_timeout(300)
    assert _count(page) == total
