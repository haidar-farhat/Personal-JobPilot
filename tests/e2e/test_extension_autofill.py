"""End-to-end autofill test: real content scripts + live backend, on a sample-forms fixture.

Injects the extension's actual scan.js / fill.js into the page (script tags) and
drives them exactly as the service worker would, then POSTs to the live
/api/autofill/plan. Proves fields fill and that the form is NEVER submitted.

Uses the shared pytest-playwright ``page`` fixture (the suite runs under
asyncio_mode=auto, so a standalone sync_playwright() context can't be used here).
Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "sample_forms.html").as_uri()
BACKEND = "http://127.0.0.1:7777"


def _plan(fields, resume_pref="ai"):
    r = httpx.post(
        f"{BACKEND}/api/autofill/plan",
        json={"url": "file://fixture", "job_title": "AI Engineer", "page_text": "",
              "resume_pref": resume_pref, "fields": fields},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def test_scan_fill_no_submit(page):
    page.goto(FIXTURE, wait_until="domcontentloaded")

    # Inject the REAL content scripts (same files the extension ships).
    page.add_script_tag(path=str(EXT / "content" / "scan.js"))
    fields = page.evaluate("window.__jpafScan()")
    assert len(fields) >= 8, f"expected to scan the forms, got {len(fields)}"

    plan = _plan(fields)
    assert plan["resume_used"] == "base_resume.yaml"

    page.add_script_tag(path=str(EXT / "content" / "fill.js"))
    stats = page.evaluate("(p) => window.__jpafApply(p)", plan)

    # Deterministic fills landed on the real inputs.
    assert page.eval_on_selector("#gh_first", "e => e.value") == "Matthew"
    assert page.eval_on_selector("#gh_last", "e => e.value") == "Cromaz"
    assert "@" in page.eval_on_selector("#gh_email", "e => e.value")
    assert page.eval_on_selector("#gh_auth", "e => e.value").lower() == "yes"
    assert page.eval_on_selector("#g_state", "e => e.value") == "California"
    assert page.eval_on_selector("#lever_name", "e => e.value") == "Matthew Cromaz"
    assert stats["filled"] >= 8

    # The résumé file input is flagged, not silently skipped.
    assert stats["file_flags"] >= 1

    # CRITICAL: the form was never submitted.
    assert page.evaluate("window.__submitted === true") is False
