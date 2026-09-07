"""Static workflow/accessibility contracts for the single-file dashboard."""

from pathlib import Path


HTML = (Path(__file__).parents[1] / "server" / "static" / "index.html").read_text(
    encoding="utf-8"
)


def test_primary_navigation_remains_keyboard_and_mobile_available():
    assert '<nav class="topnav" aria-label="Primary">' in HTML
    assert 'role="button" tabindex="0" aria-current="page"' in HTML
    mobile = HTML.split("@media(max-width:1100px)", 1)[1].split("@media print", 1)[0]
    assert ".topnav{display:flex" in mobile
    assert ".topnav,.topicons{display:none}" not in mobile


def test_mobile_filters_have_disclosure_state_and_visible_focus_target():
    assert 'id="filterBtn" aria-expanded="false" aria-controls="rail"' in HTML
    assert 'id="rail" aria-label="Job filters" tabindex="-1"' in HTML
    assert 'filterBtn.setAttribute("aria-expanded", String(open))' in HTML


def test_results_and_runtime_feedback_are_announced():
    assert 'id="resultCount" role="status" aria-live="polite"' in HTML
    assert 'id="health" role="status" aria-live="polite"' in HTML
    assert 't.setAttribute("role", isErr?"alert":"status")' in HTML


def test_job_cards_are_keyboard_operable_with_named_actions():
    assert 'role="button" tabindex="0" aria-label="${esc((a.title' in HTML
    assert 'aria-label="Save ${esc(a.title||"this job")}"' in HTML
    assert 'aria-label="Pass on ${esc(a.title||"this job")}"' in HTML
    assert 'e.key==="Enter"||e.key===" "' in HTML


def test_applied_copy_requires_manual_submission_and_recording():
    assert "submit it yourself, then use Log apply" in HTML
    assert "Apply to a job from Home and it moves here automatically" not in HTML
