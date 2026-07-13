"""Agent preferences intake + Jack&Jill-style ranking surfaces (live)."""

import pytest
import requests

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _restore_real_prefs(base_url):
    """These tests write the real config file — put the user's prefs back after."""
    before = requests.get(f"{base_url}/api/agent/preferences", timeout=5).json()
    yield
    requests.post(f"{base_url}/api/agent/preferences", json={
        "love_keywords": before.get("love_keywords") or [],
        "avoid_keywords": before.get("avoid_keywords") or [],
        "locations": before.get("locations") or "",
        "min_hourly": before.get("min_hourly"),
        "notes": before.get("notes") or "",
    }, timeout=5)


def test_prefs_roundtrip(base_url):
    payload = {
        "love_keywords": ["llm agents", "python"],
        "avoid_keywords": ["cold calling"],
        "locations": "San Francisco, Remote",
        "min_hourly": 30,
        "notes": "e2e roundtrip",
    }
    r = requests.post(f"{base_url}/api/agent/preferences", json=payload, timeout=5)
    assert r.status_code == 200 and r.json()["ok"]
    d = requests.get(f"{base_url}/api/agent/preferences", timeout=5).json()
    assert d["love_keywords"] == ["llm agents", "python"]
    assert d["avoid_keywords"] == ["cold calling"]
    assert d["min_hourly"] == 30
    assert d["notes"] == "e2e roundtrip"


def test_prefs_rejects_bad_min_hourly(base_url):
    r = requests.post(
        f"{base_url}/api/agent/preferences", json={"min_hourly": "not-a-number"}, timeout=5
    )
    assert r.status_code == 400
    # restore a sane profile after the negative test
    requests.post(
        f"{base_url}/api/agent/preferences",
        json={"love_keywords": [], "avoid_keywords": [], "locations": "", "min_hourly": None, "notes": ""},
        timeout=5,
    )


def test_prefs_modal_saves_from_ui(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    page.click("#prefsBtn")
    assert not page.locator("#prefsModal").is_hidden()
    page.fill("#prefLove", "llm, evaluation")
    page.fill("#prefAvoid", "door-to-door")
    page.fill("#prefFloor", "30")
    page.click("#prefsSave")
    page.wait_for_selector("#prefsModal", state="hidden", timeout=5000)
    d = requests.get(f"{base_url}/api/agent/preferences", timeout=5).json()
    assert d["love_keywords"] == ["llm", "evaluation"]
    assert d["avoid_keywords"] == ["door-to-door"]
    assert d["min_hourly"] == 30


def test_top_picks_strip_renders(page, base_url):
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    # Strip renders when >=3 fresh jobs score >=60 — check consistency with data
    qualifying = page.evaluate(
        "state.apps.filter(a => ['found','scored'].includes(a.status) && (a.fit_score||0) >= 60).length"
    )
    picks = page.locator("#results .pick").count()
    if qualifying >= 3:
        assert 3 <= picks <= 5
        # every pick shows a score chip and save/pass actions
        assert page.locator("#results .pick .pick-actions").count() == picks
    else:
        assert picks == 0


def test_no_console_errors_with_prefs(page, base_url):
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".card", timeout=15000)
    page.wait_for_timeout(1500)
    real = [e for e in errors if "favicon" not in e.lower()]
    assert not real, f"console errors: {real}"
