"""Interactive UI tests — filter pills, search, drawer, status updates."""

import pytest
import requests


pytestmark = pytest.mark.live


def test_filter_pill_click_toggles_active_class(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)

    applied_pill = page.locator('button.filter-pill[data-filter="applied"]')
    applied_pill.click()
    page.wait_for_timeout(500)

    classes = applied_pill.get_attribute("class") or ""
    assert "active" in classes.split(), f"applied pill should be active after click, got: {classes!r}"

    all_pill = page.locator('button.filter-pill[data-filter="all"]')
    all_classes = all_pill.get_attribute("class") or ""
    assert "active" not in all_classes.split(), "only one pill should be active at a time"


def test_search_input_accepts_text(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1000)
    search = page.locator("#search-input")
    search.fill("data analyst")
    page.wait_for_timeout(800)  # debounce window
    assert search.input_value() == "data analyst"


def test_clicking_app_row_opens_drawer_when_data_present(page, base_url):
    """If there's at least one app, clicking it should open the drawer."""
    # Skip via the API (DOM rows include a placeholder <tr> for the empty state)
    apps = requests.get(f"{base_url}/api/applications?limit=1", timeout=5).json()
    if not apps:
        pytest.skip("no application data to test drawer interaction")

    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_timeout(2500)  # let SSE deliver the initial app list

    real_rows = page.locator("#app-table-body tr[data-id]")
    if real_rows.count() == 0:
        pytest.skip("apps exist in API but DOM has not rendered them yet")

    real_rows.first.click()
    page.wait_for_timeout(400)

    overlay = page.locator("#drawer-overlay")
    classes = overlay.get_attribute("class") or ""
    assert "open" in classes.split(), f"drawer should open after row click, got: {classes!r}"


def test_status_post_endpoint_round_trips(base_url):
    """Smoke-test the POST /api/application/{id}/status endpoint without UI."""
    apps = requests.get(f"{base_url}/api/applications?limit=1", timeout=5).json()
    if not apps:
        pytest.skip("no applications available to test status update")

    app_id = apps[0]["id"]
    original_status = apps[0]["status"]

    # Round-trip: set to scored, then back to original
    r1 = requests.post(
        f"{base_url}/api/application/{app_id}/status",
        json={"status": "scored"},
        timeout=5,
    )
    assert r1.status_code == 200
    assert r1.json()["new_status"] == "scored"

    r2 = requests.post(
        f"{base_url}/api/application/{app_id}/status",
        json={"status": original_status},
        timeout=5,
    )
    assert r2.status_code == 200


def test_notes_post_endpoint_persists(base_url):
    apps = requests.get(f"{base_url}/api/applications?limit=1", timeout=5).json()
    if not apps:
        pytest.skip("no applications available to test notes update")

    app_id = apps[0]["id"]
    original_notes = apps[0].get("notes", "")
    test_note = "qa-test-marker-9c7b1f"

    try:
        r = requests.post(
            f"{base_url}/api/application/{app_id}/notes",
            json={"notes": test_note},
            timeout=5,
        )
        assert r.status_code == 200
        # verify by GET
        detail = requests.get(f"{base_url}/api/application/{app_id}", timeout=5).json()
        assert detail["notes"] == test_note
    finally:
        # restore original notes
        requests.post(
            f"{base_url}/api/application/{app_id}/notes",
            json={"notes": original_notes},
            timeout=5,
        )
