# JobPilot Autofill Extension — Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A Manifest V3 browser extension that agentically autofills job-application fields from Matthew's résumé/profile via the local JobPilot backend; the user reviews and clicks Apply (never auto-submits).

**Architecture:** Backend gains a pure mapper (`agents/autofill_mapper.py`) + a FastAPI router (`server/autofill.py`) reusing `auto_applier.base.load_profile`, `ranker._archetype_config/_match_archetype_by_title/_load_resume_summary`, and `utils.ollama_client.generate_text/check_ollama_health`. A build-free MV3 extension (`browser-extension/`) scans the page, asks the backend for a fill-plan, and applies it React-safely.

**Tech Stack:** Python 3 / FastAPI / Ollama (Gemma) backend; vanilla JS MV3 extension; Playwright (Python) for tests.

## Global Constraints
- Backend local-only (`127.0.0.1:7777`); all data stays on-machine; no external calls except the local backend/Ollama.
- Reuse, never duplicate: `load_profile()`, `_archetype_config()`, `_match_archetype_by_title()`, `_load_resume_summary()`, `generate_text()`, `check_ollama_health()`.
- Personal YAMLs are gitignored — never commit `applicant_profile.yaml` / `base_resume*.yaml`. Extension fetches them at runtime.
- Extension NEVER clicks submit; no CAPTCHA solving; on-demand injection only.
- Don't break existing dashboard endpoints/tests.

---

### Task 1: Backend deterministic field mapper (pure)

**Files:**
- Create: `agents/autofill_mapper.py`
- Test: `tests/test_autofill_mapper.py`

**Interfaces — Produces:**
- `normalize(s: str) -> str` — lowercase, strip non-alphanumeric to spaces, collapse whitespace.
- `choose_archetype(title, page_text, archetypes, resume_pref="auto") -> str | None` — `resume_pref` "ai"→None, "bt"→"behavioral_technician"; else keyword-match title then page_text via archetype keywords.
- `map_standard_field(field: dict, profile: dict) -> dict | None` — returns `{"value", "source":"deterministic", "confidence":0..1, "needs_review":bool}` or None. `field` = `{id,label,name,type,options,required}`.
- `build_plan(fields, profile, archetype, resume_summary, essay_fn=None, max_essays=3) -> dict` — deterministic first; for unmapped free-text/unknown selects call `essay_fn(field, context) -> str|None` (injectable; None in tests w/o LLM); returns `{"fields":[{id,value,source,confidence,needs_review}], "stats":{filled,needs_review,llm_used}}`.

**Mapping categories** (match against `normalize(label)` + `name`): first_name, last_name, full_name, email, phone, city, state, zip/postal, country, linkedin, github, portfolio/website, work_authorization (yes/no from `work_authorization.authorized_to_work_us`), sponsorship (`requires_sponsorship`), citizenship, years_experience, highest_education, current_employer, current_title, desired_salary (`salary.preferred_text`), referral ("how did you hear"), EEOC gender/race/veteran/disability/hispanic. File inputs → `{value:None, needs_review:True, source:"file"}`.

- [ ] **Step 1: Failing tests**
```python
# tests/test_autofill_mapper.py
from agents.autofill_mapper import normalize, choose_archetype, map_standard_field, build_plan

PROFILE = {
    "identity": {"first_name":"Matthew","last_name":"Cromaz","full_name":"Matthew Cromaz",
                 "email":"m@x.com","phone":"555-0142"},
    "address": {"city":"Oakland","state":"CA","postal_code":"94601","country":"United States"},
    "links": {"linkedin":"https://li/in/x","github":"https://gh/x"},
    "work_authorization": {"authorized_to_work_us":True,"requires_sponsorship":False,"citizenship":"U.S. Citizen"},
    "eeoc": {"gender":"Decline to answer"},
    "salary": {"preferred_text":"Open / negotiable"},
    "referral": {"default_source":"LinkedIn"},
}

def test_normalize():
    assert normalize("First  Name*") == "first name"

def test_map_email():
    f = {"id":"f1","label":"Email Address","name":"email","type":"email"}
    assert map_standard_field(f, PROFILE)["value"] == "m@x.com"

def test_map_first_name():
    f = {"id":"f2","label":"First Name","name":"first_name","type":"text"}
    assert map_standard_field(f, PROFILE)["value"] == "Matthew"

def test_map_work_auth_yesno():
    f = {"id":"f3","label":"Are you authorized to work in the US?","name":"auth","type":"select",
         "options":["Yes","No"]}
    assert map_standard_field(f, PROFILE)["value"] == "Yes"

def test_unknown_field_returns_none():
    f = {"id":"f9","label":"Favorite color","name":"color","type":"text"}
    assert map_standard_field(f, PROFILE) is None

def test_choose_archetype_bt_by_title():
    arch = {"behavioral_technician":{"keywords":["behavior technician","rbt","aba"]},
            "ai_engineer":{"keywords":["ai engineer"]}}
    assert choose_archetype("Behavior Technician", "", arch) == "behavioral_technician"

def test_choose_archetype_pref_override():
    arch = {"behavioral_technician":{"keywords":["aba"]}}
    assert choose_archetype("ABA role", "", arch, resume_pref="ai") is None

def test_build_plan_routes_essays_to_fn():
    fields = [{"id":"f1","label":"Email","name":"email","type":"email"},
              {"id":"f2","label":"Why do you want to work here?","name":"why","type":"textarea"}]
    seen = []
    def essay_fn(field, ctx): seen.append(field["id"]); return "Because reasons."
    plan = build_plan(fields, PROFILE, None, "RESUME", essay_fn=essay_fn)
    byid = {f["id"]:f for f in plan["fields"]}
    assert byid["f1"]["value"] == "m@x.com" and byid["f1"]["source"]=="deterministic"
    assert byid["f2"]["value"] == "Because reasons." and byid["f2"]["source"]=="llm"
    assert seen == ["f2"] and plan["stats"]["filled"] == 2
```
- [ ] **Step 2:** Run `venv\Scripts\python -m pytest tests/test_autofill_mapper.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `agents/autofill_mapper.py` (regex category table keyed on normalized label/name; yes/no resolution picks the matching option text from `options`; `build_plan` loops fields, tries `map_standard_field`, else if type in {textarea} or label endswith "?" or unknown select → `essay_fn`; cap essays at `max_essays`).
- [ ] **Step 4:** Run tests → PASS.
- [ ] **Step 5:** Commit `feat(autofill): deterministic field mapper + archetype/résumé routing`.

---

### Task 2: Backend FastAPI router + CORS wiring

**Files:**
- Create: `server/autofill.py`
- Modify: `server/dashboard.py` (add `CORSMiddleware`, `app.include_router(autofill.router)`)
- Test: `tests/test_autofill_api.py`

**Interfaces — Consumes:** Task 1 functions; `auto_applier.base.load_profile`; `ranker._archetype_config`, `ranker._load_resume_summary`; `utils.ollama_client.generate_text`, `check_ollama_health`.
**Produces:** `router = APIRouter()` with:
- `GET /api/autofill/profile` → `load_profile()` JSON.
- `POST /api/autofill/plan` (body `AutofillRequest`) → choose archetype (`choose_archetype(title,page_text,archetypes,resume_pref)`), résumé via `_load_resume_summary(archetype)`, `build_plan(..., essay_fn=_essay_fn)` where `_essay_fn` calls `generate_text` with profile+résumé context (wrapped try/except → `fallback_essays`/None, source flips to "fallback").
- `GET /api/autofill/health` → `{ok:True, ollama_up:check_ollama_health(), profile_loaded:bool}`.

- [ ] **Step 1: Failing test**
```python
# tests/test_autofill_api.py
from fastapi.testclient import TestClient
import server.dashboard as dash
client = TestClient(dash.app)

def test_health():
    r = client.get("/api/autofill/health")
    assert r.status_code == 200 and "ollama_up" in r.json()

def test_plan_deterministic(monkeypatch):
    # no LLM: essay fields just come back needs_review
    monkeypatch.setattr("server.autofill.generate_text", lambda *a, **k: "DRAFT")
    body = {"url":"http://x","job_title":"Behavior Technician","fields":[
        {"id":"f1","label":"Email","name":"email","type":"email"},
        {"id":"f2","label":"Why this role?","name":"why","type":"textarea"}]}
    r = client.post("/api/autofill/plan", json=body)
    assert r.status_code == 200
    d = r.json(); byid = {f["id"]:f for f in d["fields"]}
    assert byid["f1"]["value"]  # email filled deterministically
    assert d["resume_used"]  # routed to a résumé
```
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement `server/autofill.py` (Pydantic `FieldSpec`, `AutofillRequest`; `_essay_fn` closure). In `dashboard.py` add near app creation:
```python
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
from server import autofill
app.include_router(autofill.router)
```
(CORS is acceptable: the server binds to 127.0.0.1 only.)
- [ ] **Step 4:** Run `pytest tests/test_autofill_api.py -q` → PASS. Restart dashboard (watchdog) to load the router.
- [ ] **Step 5:** Commit `feat(autofill): /api/autofill profile|plan|health endpoints + CORS`.

---

### Task 3: Extension scaffold — manifest, background, popup

**Files:** Create `browser-extension/manifest.json`, `browser-extension/background.js`, `browser-extension/popup.html`, `browser-extension/popup.css`, `browser-extension/popup.js`, `browser-extension/icons/icon128.png` (simple generated PNG).

**manifest.json:** MV3; `action.default_popup=popup.html`; `permissions:["activeTab","scripting","storage"]`; `host_permissions:["http://127.0.0.1:7777/*"]`; `background.service_worker="background.js"`.

**background.js:** `BACKEND="http://127.0.0.1:7777"`. Messages: `getHealth` → fetch `/api/autofill/health`; `runAutofill(tabId, resumePref)` → `chrome.scripting.executeScript({target,files:["content/scan.js"]})`, read returned fields from `window.__jpafScan()`, POST `/api/autofill/plan`, `executeScript` `content/fill.js` then call `window.__jpafApply(plan)`, return stats. (Use `func`-injection wrappers to call the exposed globals and pass/return JSON.)

**popup:** Autofill button, résumé `<select>` (Auto/AI/BT), status dot (from getHealth), result line, dashboard link. Dark-glass styling.

- [ ] **Step 1:** Write the 6 files (full code).
- [ ] **Step 2:** Generate `icons/icon128.png` via venv Python + Pillow (solid gradient "JP" square) — or a 1x1 placeholder if Pillow absent.
- [ ] **Step 3: Verify load** — `chrome://extensions` Load unpacked (manual) OR Playwright persistent-context load in Task 5. Confirm `manifest.json` parses: `venv\Scripts\python -c "import json;json.load(open('browser-extension/manifest.json'));print('ok')"`.
- [ ] **Step 4:** Commit `feat(ext): MV3 scaffold — manifest, service worker, popup`.

---

### Task 4: Content scripts — scan + fill

**Files:** Create `browser-extension/content/scan.js`, `browser-extension/content/fill.js`.

**scan.js (exposes `window.__jpafScan()` → array):** select `input,select,textarea` (skip hidden/`type` in {hidden,submit,button,image,file→include as type:"file"}); for each assign `el.dataset.jpafId="f"+i`; resolve label via `<label for>`, `aria-labelledby`, `aria-label`, `closest('label')`, `placeholder`, preceding text node; collect `options` for selects; return `{id,label,name,type,options,required}`.

**fill.js (exposes `window.__jpafApply(plan)` → stats):** for each plan field find `[data-jpaf-id]`; skip null/file values; set value React-safely:
```js
function setNative(el, value){
  const proto = el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto,"value").set;
  setter.call(el, value);
  el.dispatchEvent(new Event("input",{bubbles:true}));
  el.dispatchEvent(new Event("change",{bubbles:true}));
}
```
selects → match option by exact then normalized text, set value, dispatch change; radios/checkboxes → match by value/label then `.click()`. Outline filled green (`2px solid #0a7e07`), needs_review amber; floating toast "JobPilot filled N fields — review & click Apply (M need review)". **Never** call form.submit() or click submit-like buttons.

- [ ] **Step 1:** Write both files (full code).
- [ ] **Step 2:** Lint parse: `node --check browser-extension/content/scan.js && node --check browser-extension/content/fill.js` (Node present).
- [ ] **Step 3:** Commit `feat(ext): scan + React-safe fill content scripts`.

---

### Task 5: Sample-forms fixture + Playwright extension test + verify script

**Files:** Create `tests/e2e/fixtures/sample_forms.html`, `tests/e2e/test_extension_autofill.py`, `scripts/verify_autofill.py`.

**fixture:** three `<form>`s (Greenhouse-like: First/Last/Email/Phone/LinkedIn/GitHub/“Why us?” textarea + auth select; Lever-like names; generic). A submit button wired to set `window.__submitted=true` (so the test proves we DON'T submit).

**test (live, requires backend on :7777):**
```python
import pytest
pytestmark = pytest.mark.live
from pathlib import Path

def test_autofill_fixture(tmp_path):
    from playwright.sync_api import sync_playwright
    ext = str(Path("browser-extension").resolve())
    user = str(tmp_path/"prof")
    fixture = (Path("tests/e2e/fixtures/sample_forms.html").resolve()).as_uri()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(user, headless=False,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        pg = ctx.new_page(); pg.goto(fixture)
        pg.add_script_tag(path="browser-extension/content/scan.js")
        fields = pg.evaluate("__jpafScan()")
        import requests
        plan = requests.post("http://127.0.0.1:7777/api/autofill/plan",
            json={"url":fixture,"job_title":"Behavior Technician","fields":fields}).json()
        pg.add_script_tag(path="browser-extension/content/fill.js")
        stats = pg.evaluate("p => __jpafApply(p)", plan)
        assert pg.eval_on_selector("#gh_email","e=>e.value")  # email filled
        assert stats["filled"] >= 4
        assert pg.evaluate("window.__submitted") in (None, False)  # never submitted
        ctx.close()
```
**verify_autofill.py:** same flow headless-where-possible, prints `[Autofill] | Fields: N | Filled: M | Needs review: K | Submitted: NO`.

- [ ] **Step 1:** Write fixture + test + script.
- [ ] **Step 2:** Run `venv\Scripts\python -m pytest tests/e2e/test_extension_autofill.py -q -m live` (backend up) → PASS.
- [ ] **Step 3:** Run `verify_autofill.py`, capture the line.
- [ ] **Step 4:** Commit `test(ext): sample-forms fixture + Playwright autofill e2e + verify script`.

---

### Task 6: README install docs

**Files:** Modify `README.md` (add "Browser Autofill Extension" section: what it does, ToS note, Load-unpacked steps, backend-must-run note).

- [ ] **Step 1:** Add section.
- [ ] **Step 2:** Commit `docs: README section for the autofill extension`.

---

## Self-Review
- **Spec coverage:** profile/plan/health endpoints (T2), deterministic+LLM mapping (T1), résumé auto-routing (T1 `choose_archetype`), MV3 extension popup/bg/content (T3/T4), React-safe set (T4), no-submit + file-flag (T4/T5), tests + fixture + verify (T5), install docs (T6). ✓
- **Placeholders:** none — code shown for hard parts; boilerplate files written in execution.
- **Type consistency:** plan field shape `{id,label,name,type,options,required}` and result `{id,value,source,confidence,needs_review}` identical across mapper, API, scan.js, fill.js. ✓
