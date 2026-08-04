"""E2E: Workday wizard — fill step 1, auto-advance, fill step 2, STOP at Submit.

Drives the REAL content scripts (workday.js / scan.js / fill.js) on the
qa_workday.html 3-step fixture served by the dashboard. Proves the multi-page
loop: structured sections fill, __jpafWorkdayNext clicks "Save and Continue",
the disclosure step fills from the profile presets, and the engine REFUSES to
click the final Submit button.

Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
BACKEND = "http://127.0.0.1:7777"
FIXTURE = f"{BACKEND}/static/qa_workday.html"


def _profile():
    """Live profile — expected values are read from it, never hardcoded here."""
    r = httpx.get(f"{BACKEND}/api/autofill/profile", timeout=30)
    r.raise_for_status()
    return r.json()

CHROME_STUB = """
window.chrome = window.chrome || {};
window.chrome.runtime = {
  sendMessage: (msg, cb) => {
    (async () => {
      try {
        if (msg.cmd === "profile") {
          const r = await fetch("/api/autofill/profile");
          cb(await r.json());
        } else if (msg.cmd === "history") {
          const r = await fetch("/api/autofill/history?resume_pref=ai");
          cb(await r.json());
        } else if (msg.cmd === "resume_file") {
          const r = await fetch("/api/autofill/resume_file?resume_pref=ai");
          const buf = await r.arrayBuffer();
          let bin = "";
          const bytes = new Uint8Array(buf);
          for (let i = 0; i < bytes.length; i += 0x8000)
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
          cb({ b64: btoa(bin), filename: "Matthew_Cromaz_Resume.pdf",
               mime: r.headers.get("content-type") || "application/pdf" });
        } else cb(null);
      } catch (e) { cb(null); }
    })();
    return true;
  },
};
"""


def _plan(fields):
    r = httpx.post(
        f"{BACKEND}/api/autofill/plan",
        json={"url": "https://acme.myworkdayjobs.com/en-US/careers/job/x/apply",
              "job_title": "Analytics Engineer", "company": "acme",
              "page_text": "", "resume_pref": "ai", "fields": fields},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def test_workday_multipage_advance_and_stop_at_submit(page):
    page.goto(FIXTURE, wait_until="domcontentloaded")
    page.evaluate(CHROME_STUB)
    for f in ("workday.js", "scan.js", "fill.js"):
        page.add_script_tag(path=str(EXT / "content" / f))
    # the hostname gate is environmental — the fixture serves from localhost
    page.evaluate("window.__jpafIsWorkday = () => true")

    # Poison the Phone Extension field with the applicant's own number, the way
    # a Workday résumé-parse does. Seeded here rather than in the fixture so the
    # real number never lands in this public repo.
    phone = _profile()["identity"]["phone"]
    page.eval_on_selector("#mi-ext", "(e, v) => { e.value = v; }", phone)

    # ---- the widget's multi-page loop, driven explicitly ----
    pages, total, last_next = 1, 0, None
    for _ in range(5):
        ws = page.evaluate("() => window.__jpafWorkdayRun()")
        total += (ws or {}).get("filled", 0)
        fields = page.evaluate("() => window.__jpafScan()")
        if fields:
            plan = _plan(fields)
            page.evaluate(
                "([p, f]) => { p._scanMeta = f; p._ctx = {company: 'acme', title: 'AE'};"
                " return window.__jpafApply(p); }",
                [plan, fields])
        last_next = page.evaluate("() => window.__jpafWorkdayNext()")
        if not last_next or not last_next.get("clicked"):
            break
        pages += 1

    # advanced 1 → 2 → 3, then refused the Submit button on the review step
    assert pages == 3, f"expected to land on step 3, got {pages} ({last_next})"
    assert last_next and not last_next.get("clicked")
    assert str(last_next.get("reason", "")).startswith("at-"), last_next
    assert page.eval_on_selector("#navNext", "e => e.innerText.trim()") == "Submit"

    # ---- step 1 filled: structured sections ----
    def v(sel):
        return page.eval_on_selector(sel, "e => e.value")

    # ---- work experience is POSITION-OWNED (Cisco regression 2026-07-06):
    # history order with the newest job on TOP, parser leftovers overwritten,
    # a panel ADDED for the job the parser missed ----
    def wpanel(n):
        return f"div[role='group'][aria-labelledby='Work-Experience-{n}-panel']"

    def wv(n, fid):
        return page.eval_on_selector(
            f"{wpanel(n)} [data-automation-id='formField-{fid}'] input, "
            f"{wpanel(n)} [data-automation-id='formField-{fid}'] textarea", "e => e.value")

    def wdate(n, which, part):
        return page.eval_on_selector(
            f"{wpanel(n)} [data-automation-id='formField-{which}'] "
            f"[data-automation-id='dateSection{part}-input']", "e => e.value")

    def wcur(n):
        return page.eval_on_selector(
            f"{wpanel(n)} [data-automation-id='formField-currentlyWorkHere'] input",
            "e => e.checked")

    n_work = page.eval_on_selector_all(
        "div[role='group'][aria-labelledby^='Work-Experience-'][aria-labelledby$='-panel']",
        "els => els.length")
    assert n_work == 4, f"expected 4 work panels, got {n_work}"
    # slot 1: the CURRENT job — it was missing entirely on the live Cisco draft
    assert wv(1, "jobTitle") == "Freelance Software Engineer"
    assert wv(1, "companyName") == "Leasing Agent 415 (Compass)"
    assert wcur(1) is True
    assert (wdate(1, "startDate", "Month"), wdate(1, "startDate", "Year")) == ("06", "2026")
    # slot 2: BIA — the OTHER current job, right after the newest one
    # (reverse-chron history order, 2026-07-22); current → End Date group removed
    assert wv(2, "companyName") == "Behavioral Intervention Associates (BIA)"
    assert wv(2, "location") == "San Mateo, CA"
    assert wcur(2) is True
    assert page.eval_on_selector_all(
        f"{wpanel(2)} [data-automation-id='formField-endDate']", "els => els.length") == 0
    # slot 3: parser's "Rithum | Remote" mashup overwritten, end date landed
    assert wv(3, "companyName") == "Rithum"
    assert wv(3, "location") == "Remote"
    assert (wdate(3, "endDate", "Month"), wdate(3, "endDate", "Year")) == ("06", "2025")
    # slot 4: ADDED via Add Another for the item the parser never created
    assert wv(4, "companyName").startswith("Reed College")
    assert (wdate(4, "endDate", "Month"), wdate(4, "endDate", "Year")) == ("12", "2023")
    assert page.eval_on_selector_all("[data-automation-id^='education-']", "els => els.length") >= 2
    assert v("[data-automation-id='education-1'] [data-automation-id='school']").startswith("California Polytechnic")
    assert v("[data-automation-id='education-1'] [data-automation-id='gpa']") == "3.467"
    assert page.eval_on_selector_all("[data-automation-id='selectedItem']", "els => els.length") >= 5
    assert page.eval_on_selector("#fileIn", "e => e.files.length") == 1
    assert "Resume" in page.eval_on_selector("#fileIn", "e => e.files[0].name")

    # ---- step 1 filled: "My Information" block (NVIDIA-shaped) ----
    checked_pw = page.evaluate(
        "() => [...document.querySelectorAll('input[name=prevWorker]')].find(r => r.checked)?.value")
    assert checked_pw == "false"                     # never worked at this company
    # exact-first matching: NOT "United States Minor Outlying Islands"
    assert page.eval_on_selector("#myinfoCountry", "e => e.innerText.trim()") == "United States of America"
    assert page.eval_on_selector("#myinfoState", "e => e.innerText.trim()") == "California"
    assert page.eval_on_selector("#myinfoPhoneType", "e => e.innerText.trim()") == "Mobile"
    # Country Phone Code commits via the searchable prompt (selectedItem chip);
    # the pre-seeded bogus "Afghanistan (+93)" chip must be REPLACED
    cpc_text = page.eval_on_selector("#cpcPills", "e => e.innerText")
    assert "United States of America (+1)" in cpc_text
    assert "Afghanistan" not in cpc_text
    assert v("#mi-first") == "Matthew"
    assert v("#mi-addr1") == _profile()["address"]["street"]
    # stale full address parked in Line 2 must be CLEARED
    assert v("#mi-addr2") == ""
    assert v("#mi-city") == "San Francisco"
    assert v("#mi-postal") == "94122"
    # the pre-filled formatted number is rewritten to bare national digits
    import re as _re
    assert v("#mi-phone") == _re.sub(r"\D", "", _profile()["identity"]["phone"])
    # the extension field ships pre-poisoned with the phone number — must be CLEARED
    assert v("#mi-ext") == ""

    # ---- step 2 filled: disclosures from the JobRight-profile presets ----
    def btn_text(aid):
        return page.eval_on_selector(f"[data-automation-id='{aid}']", "e => e.innerText.trim()")

    assert btn_text("gender") == "Male"
    assert "Two or More Races" in btn_text("ethnicity")
    assert btn_text("veteranStatus") == "I am not a protected veteran"
    checked_hispanic = page.evaluate(
        "() => [...document.querySelectorAll('input[name=hispanic]')].find(r => r.checked)?.value")
    assert checked_hispanic == "Not Hispanic or Latino"
    checked_dis = page.evaluate(
        "() => [...document.querySelectorAll('input[name=disability]')].find(r => r.checked)?.value")
    assert checked_dis == "Yes"
    assert page.eval_on_selector("[data-automation-id='formField-selfIdentifyName'] input", "e => e.value") == "Matthew Cromaz"
    assert page.eval_on_selector("[data-automation-id='termsCheckbox']", "e => e.checked") is True

    # CRITICAL: Submit was never clicked.
    assert page.evaluate("window.__submitted === true") is False
