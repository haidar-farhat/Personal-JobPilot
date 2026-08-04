"""E2E: Greenhouse job-boards form (react-select clone of the real DoorDash DOM).

The fixture replicates what job-boards.greenhouse.io actually renders: 3px-wide
react-select inputs (role=combobox), per-widget option portals with
react-select-<id>-option-N ids, non-searchable (readOnly-input) selects, a
multi-select Gender, aria-hidden shadow "required" inputs, visually-hidden file
inputs behind Attach buttons, and a DECOY unrelated option list (like
intl-tel-input's country menu).

Passing requires values to actually COMMIT (single-value/multi-value chips
rendered), the decoy to never be clicked, the résumé to attach via
DataTransfer, and the form to never submit.

Requires the JobPilot dashboard running at http://127.0.0.1:7777.
"""

from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.live

_ROOT = Path(__file__).resolve().parents[2]
EXT = _ROOT / "browser-extension"
BACKEND = "http://127.0.0.1:7777"
FIXTURE = f"{BACKEND}/static/qa_greenhouse.html"


def _profile():
    """Live profile — expected values are read from it, never hardcoded here."""
    r = httpx.get(f"{BACKEND}/api/autofill/profile", timeout=30)
    r.raise_for_status()
    return r.json()


# greenhouse.js + fill.js talk to the service worker; stub it onto the live API.
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
        } else if (msg.cmd === "resume_file" || msg.cmd === "cover_letter_file") {
          const ep = msg.cmd === "resume_file" ? "resume_file" : "cover_letter_file";
          const r = await fetch(`/api/autofill/${ep}?resume_pref=ai&company=${encodeURIComponent(msg.company || "")}&job_title=${encodeURIComponent(msg.title || "")}`);
          if (!r.ok) { cb({ error: "HTTP " + r.status }); return; }
          const buf = await r.arrayBuffer();
          let bin = "";
          const bytes = new Uint8Array(buf);
          for (let i = 0; i < bytes.length; i += 0x8000)
            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
          const disp = r.headers.get("content-disposition") || "";
          const m = disp.match(/filename="?([^";]+)"?/);
          cb({ b64: btoa(bin), filename: m ? m[1] : "file.pdf",
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
        json={"url": "https://job-boards.greenhouse.io/doordashusa/jobs/7028089",
              "job_title": "Analytics Engineer", "company": "doordashusa",
              "page_text": "", "resume_pref": "ai", "fields": fields},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()


def test_greenhouse_react_select_commit_and_attach(page):
    page.goto(FIXTURE, wait_until="domcontentloaded")
    page.evaluate(CHROME_STUB)
    for f in ("greenhouse.js", "scan.js", "fill.js"):
        page.add_script_tag(path=str(EXT / "content" / f))

    # committed value = the chip react-select renders, NOT the typed text
    def chip(el_id):
        return page.evaluate(
            "(id) => { const i = document.getElementById(id); if (!i) return null;"
            " const s = i.closest('.select-shell').querySelector('.select__single-value');"
            " return s ? s.textContent : null; }", el_id)

    def chips(el_id):
        return page.evaluate(
            "(id) => [...document.getElementById(id).closest('.select-shell')"
            ".querySelectorAll('.select__multi-value')].map((c) => c.textContent)", el_id)

    def v(sel):
        return page.eval_on_selector(sel, "e => e.value")

    # ---- 1. Greenhouse engine: multi-entry education + employment history ----
    assert set(page.evaluate("() => window.__jpafGreenhouseSections()")) == {"education", "work"}
    gh = page.evaluate("() => window.__jpafGreenhouseRun()")
    assert gh and gh.get("sections", {}).get("education", 0) >= 9, f"education underfilled: {gh}"
    assert gh.get("sections", {}).get("work", 0) >= 15, f"employment underfilled: {gh}"

    assert chip("school--0") == "California Polytechnic State University-San Luis Obispo"
    assert chip("degree--0") == "Master's Degree"          # readOnly input: open+click path
    assert "Economics" in chip("discipline--0")
    assert v("#gpa--0") == "3.467"
    assert v("#end-month--0") == "June"
    assert v("#end-year--0") == "2025"
    assert chip("school--1") == "Reed College"             # added via "Add another"
    assert chip("degree--1") == "Bachelor's Degree"
    assert v("#end-year--1") == "2024"

    # employment history from the base résumé, most recent first. Dates are the
    # real Coinbase-embed shape: month = react-select COMBO (chip), year = text.
    assert v("#company--0") == "Leasing Agent 415 (Compass)"
    assert v("#title--0") == "Freelance Software Engineer"
    assert page.eval_on_selector("#current--0", "e => e.checked") is True
    assert chip("start-month--0") == "June"
    assert v("#start-year--0") == "2026"
    assert chip("w-end-month--0") is None                  # current role: end date skipped
    assert v("#company--1") == "Behavioral Intervention Associates (BIA)"
    assert v("#title--1") == "Behavioral Technician"
    assert v("#company--2") == "Rithum"
    assert v("#title--2") == "Economic Consultant (Graduate Capstone)"
    assert chip("start-month--2") == "March"
    assert v("#w-end-year--2") == "2025"
    assert v("#company--3") == "Reed College Finance & Investment Club"

    # ---- 2. flat pass: scan → live plan → apply ----
    fields = page.evaluate("() => window.__jpafScan()")
    # engine owns education + employment; shadow required inputs and decoys excluded
    assert not any("school" in (f.get("name") or "") for f in fields)
    assert not any("company--" in (f.get("name") or "") for f in fields)
    assert not any("requiredInput" in (f.get("name") or "") + (f.get("label") or "") for f in fields)
    plan = _plan(fields)
    stats = page.evaluate(
        "([p, fields]) => { p._scanMeta = fields;"
        " p._ctx = {company: 'doordashusa', title: 'Analytics Engineer'};"
        " return window.__jpafApply(p); }",
        [plan, fields],
    )

    # Expected values come from the live profile — this repo is public, so a
    # real email or phone number must never be hardcoded into an assertion.
    ident = _profile()["identity"]
    assert v("#first_name") == ident["first_name"]
    assert v("#last_name") == ident["last_name"]
    assert v("#email") == ident["email"]
    assert v("#phone") == ident["phone"]
    assert v("#question_linkedin") == _profile()["links"]["linkedin"]

    # react-select COMMITS (chips), including the async location picker
    assert chip("country") == "United States"
    assert chip("candidate-location") == "San Francisco, California, United States"
    assert chip("question_58117065") == "Yes"
    assert chip("question_58117066") == "No"
    assert chip("question_58117067") == "I have not worked at DoorDash"
    assert chip("question_58117068") == "Yes"
    assert chip("question_58117069") == "Yes"
    assert chip("question_58117070") == "No"

    # Coinbase-embed-shaped screeners: single-option consent vocabularies commit
    # via yes-affirmative matching; verbose government options via leading "No";
    # the "institutional client" referral question must NOT become a school.
    assert chip("question_67781422") == "Yes"                # at least 18
    assert chip("question_67781423") == "No"                 # previously employed by
    assert chip("question_67781425") == "Confirmed"          # privacy notice receipt
    assert chip("question_67781426") == "Yes"                # "I understand … AI tools"
    assert chip("question_67781427") == ("I design or automate workflows with AI tools "
                                         "(e.g., building agents, integrating AI into team processes).")
    assert chip("question_67781430") == "No, I am not a current or former Government Official"
    assert chip("question_67781431") == "No, I am not a relative of a government official."
    assert chip("question_67781433") == "No"                 # referred by senior leader

    # availability + current-location free-text questions (deterministic)
    assert v("#q_current_location") == "San Francisco, California, United States"
    assert v("#q_start_when") == "September 1, 2026"

    # open-ended textarea routes to the LLM essay path: drafted from the JD +
    # essay_facts when Ollama is up, flagged for review either way
    why = next(a for a in plan["fields"]
               if next(f for f in fields if f["id"] == a["id"]).get("name") == "q_why")
    assert why["needs_review"] is True
    assert why["source"] in ("llm", "none", "fallback")
    if why["value"]:
        assert v("#q_why") == why["value"]

    # demographics (JobRight-profile presets) — Gender is a multi-select
    assert chips("864") == ["Male"]
    assert chip("1328") == "No"
    assert chip("1332") == "No"
    assert chip("1333") == "Two or more races"             # readOnly input: open+click path
    assert chip("1336") == "I am not a protected veteran"
    assert chip("1337") == "Yes, I have (or have previously had) a disability"
    assert chip("q_lgbtq") == "No"

    checked = page.evaluate(
        "() => [...document.querySelectorAll('input[name=demo_orientation]')]"
        ".filter(c => c.checked).map(c => c.value)")
    assert checked == ["Heterosexual (straight)"]

    # race "select all that apply": half East Asian / half White → BOTH boxes
    # checked, nothing else (the single-select above keeps "Two or more races")
    race_checked = page.evaluate(
        "() => [...document.querySelectorAll('input[name=demo_race]')]"
        ".filter(c => c.checked).map(c => c.value)")
    assert race_checked == ["Asian", "White"]

    # ---- 3. résumé attached via DataTransfer; decoy options never touched ----
    assert page.eval_on_selector("#resume", "e => e.files.length") == 1
    # attached under the same name the widget pill shows (the uploaded original)
    meta = httpx.get(f"{BACKEND}/api/autofill/resume_meta?resume_pref=ai", timeout=10).json()
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == meta["serve_name"]
    assert page.eval_on_selector("#resume", "e => e.files[0].size") > 50000
    assert stats.get("resume_attached") == 1
    # no tailored cover letter exists for "doordashusa" — left for manual attach
    assert page.eval_on_selector("#cover_letter", "e => e.files.length") == 0
    assert page.evaluate("window.__decoyClicks") == 0

    # CRITICAL: the form was never submitted.
    assert page.evaluate("window.__submitted === true") is False
