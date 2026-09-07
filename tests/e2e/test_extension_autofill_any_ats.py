"""The /goal test: autofill works on ANY application form, and never lies.

Drives the REAL content scripts (scan.js + fill.js) and the REAL
/api/autofill/plan against six form shapes on one page — Lever, Ashby,
SmartRecruiters, iCIMS, Workable, and a hand-rolled custom careers form with no
ATS markup at all.

Two halves, and the second matters more:

  test_fills_every_ats_shape   — the right values land in the right inputs
  test_never_fills_a_trap      — fields with NO correct answer stay EMPTY

The trap form is the anti-hallucination contract. Each field is a documented
way the mapper can produce a confident wrong answer: "Emergency contact phone"
inheriting the applicant's own number, "Referral code" taking a referral
SOURCE, a degree dropdown offering only degrees he doesn't hold. A wrong answer
in any of them is a misrepresentation on a real job application, which is worse
than an empty box the user fills in himself.

Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
FIXTURE = (Path(__file__).resolve().parent / "fixtures" / "multi_ats_forms.html").as_uri()
BACKEND = "http://127.0.0.1:7777"

# Every trap field, and the value it must never be given.
TRAPS = {
    "#tp_ec_phone": "the applicant's own phone number",
    "#tp_ec_name": "the applicant's own name",
    "#tp_ref_email": "the applicant's own email",
    "#tp_refcode": "a referral source such as LinkedIn",
    "#tp_degree": "a degree he does not hold",
    "#tp_school": "a university he did not attend",
    "#tp_major": "a field of study he did not study",
    "#tp_otp": "LLM prose in a verification-code box",
    "#tp_ssn": "a social security number",
}


def _plan(fields):
    r = httpx.post(
        f"{BACKEND}/api/autofill/plan",
        json={"url": "file://multi_ats", "job_title": "AI Engineer", "company": "",
              "page_text": "", "resume_pref": "ai", "fields": fields},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


@pytest.fixture
def filled(page):
    """Scan → plan → fill the whole fixture once; hand back the page."""
    page.goto(FIXTURE, wait_until="domcontentloaded")
    page.add_script_tag(path=str(EXT / "content" / "scan.js"))
    fields = page.evaluate("window.__jpafScan()")
    assert len(fields) >= 30, f"scanner missed forms — only saw {len(fields)} fields"

    plan = _plan(fields)
    plan["_scanMeta"] = fields
    plan["_ctx"] = {"company": "", "title": "AI Engineer"}
    page.add_script_tag(path=str(EXT / "content" / "fill.js"))
    page.evaluate("(p) => window.__jpafApply(p)", plan)
    return page


def val(page, sel):
    return page.eval_on_selector(sel, "e => e.value")


# ---------------------------------------------------------------- coverage

@pytest.fixture(scope="module")
def profile():
    """Expected values come from the live profile, never hardcoded.

    This repo is public. Baking a real phone number or street address into an
    assertion publishes it, and it silently rots the moment the profile changes.
    """
    r = httpx.get(f"{BACKEND}/api/autofill/profile", timeout=30)
    r.raise_for_status()
    return r.json()


# selector -> where its expected value lives in the profile
EXPECTED = {
    # Lever — placeholder-only labels, no <label for>
    "#lv_name": ("identity", "full_name"),
    "#lv_email": ("identity", "email"),
    "#lv_phone": ("identity", "phone"),
    # Ashby — aria-label only
    "#ab_name": ("identity", "full_name"),
    "#ab_email": ("identity", "email"),
    # SmartRecruiters — split names, verbose country vocabulary
    "#sr_first": ("identity", "first_name"),
    "#sr_last": ("identity", "last_name"),
    "#sr_email": ("identity", "email"),
    "#sr_zip": ("address", "postal_code"),
    # iCIMS — legacy table layout, label in a sibling <td>
    "#ic_name": ("identity", "full_name"),
    "#ic_street": ("address", "street"),
    "#ic_city": ("address", "city"),
    "#ic_state": ("address", "state_full"),
    # Workable
    "#wk_first": ("identity", "first_name"),
    "#wk_last": ("identity", "last_name"),
    "#wk_email": ("identity", "email"),
    # Bespoke custom form — no ATS markup whatsoever
    "#cu_email": ("identity", "email"),
}


@pytest.mark.parametrize("sel,path", list(EXPECTED.items()), ids=list(EXPECTED))
def test_fills_every_ats_shape(filled, profile, sel, path):
    section, key = path
    expected = profile[section][key]
    assert expected, f"profile has no {section}.{key} to check against"
    assert val(filled, sel) == expected


@pytest.mark.parametrize("sel", ["#lv_auth", "#cu_auth"])
def test_work_authorization_answers_yes(filled, sel):
    assert val(filled, sel) == "Yes"


def test_country_picks_the_tightest_option_not_the_first_containing_match(filled):
    """"United States" must land on "…of America", never "…Minor Outlying Islands"."""
    assert val(filled, "#sr_country") == "United States of America"


def test_linkedin_url_lands_on_the_lever_url_field(filled, profile):
    assert val(filled, "#lv_li") == profile["links"]["linkedin"]


# ---------------------------------------------------------------- traps

@pytest.mark.parametrize("sel,wrong", list(TRAPS.items()), ids=[s[1:] for s in TRAPS])
def test_never_fills_a_trap(filled, sel, wrong):
    got = val(filled, sel)
    assert got == "", (
        f"{sel} was filled with {got!r}. This field has no correct answer in the "
        f"profile — filling it means committing {wrong} on a job application. "
        f"An empty box the user completes himself is the correct outcome."
    )


def test_form_is_never_submitted(filled):
    """The entire promise: we fill, the user clicks Apply."""
    assert filled.evaluate("window.__submitted") is False


# ------------------------------------------------- the user's answers win

def test_never_overwrites_an_answer_the_user_already_gave(page, profile):
    """Autofill runs on half-completed forms all the time.

    Flipping a deliberate "No" to "Yes" on a work-authorization question is the
    worst thing this extension could do, so anything already answered is left
    exactly as the user left it and reported back as kept.
    """
    page.goto(FIXTURE, wait_until="domcontentloaded")
    typed = {"#lv_phone": "(555) 123-9999", "#sr_first": "Matt"}
    for sel, v in typed.items():
        page.eval_on_selector(sel, f"e => e.value = {v!r}")
    page.select_option("#cu_auth", "No")          # he is NOT authorized, he says

    page.add_script_tag(path=str(EXT / "content" / "scan.js"))
    fields = page.evaluate("window.__jpafScan()")
    plan = _plan(fields)
    plan["_scanMeta"] = fields
    plan["_ctx"] = {"company": "", "title": "AI Engineer"}
    page.add_script_tag(path=str(EXT / "content" / "fill.js"))
    stats = page.evaluate("(p) => window.__jpafApply(p)", plan)

    for sel, v in typed.items():
        assert val(page, sel) == v, f"{sel} overwrote the user's own answer"
    assert val(page, "#cu_auth") == "No", "autofill flipped a work-authorization answer"
    assert stats["kept"] >= 3, "kept count must report what was left alone"

    # blanks are still filled — preserving answers must not mean doing nothing
    assert val(page, "#sr_last") == profile["identity"]["last_name"]
