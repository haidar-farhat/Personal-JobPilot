# JobPilot Autofill Extension — Design Spec

**Date:** 2026-06-29
**Status:** Approved (engine, sites, essay scope confirmed by user)

## Goal
A Manifest V3 Chrome extension that **agentically autofills** job-application form fields from Matthew's résumé/profile, sourced from the local JobPilot backend (FastAPI + Ollama). The user always reviews and clicks Apply/Submit themselves — the extension never submits (ToS-safe). It is a **separate subsystem** that **reuses** the JobPilot backend, profile, and résumés.

## Non-goals (YAGNI)
- No auto-submit, no CAPTCHA solving, no auto-navigation through wizard steps.
- No Workday support in v1 (multi-step wizard, login walls).
- No programmatic résumé-file upload — browsers block JS from setting `<input type=file>`; flag + assist instead.
- No cloud LLM — local Ollama only.

## Global constraints
- Backend bound to `127.0.0.1:7777` (local only); all data stays on-machine.
- Reuse existing config + logic: `applicant_profile.yaml`, `base_resume.yaml` (AI), `base_resume_bt.yaml` (BT), `archetypes.yaml`; the ranker's archetype classifier; the Ollama Gemma client; `fallback_essays`.
- Personal résumé/profile YAMLs are **gitignored** — never commit them. The extension fetches them at runtime from the backend.
- Must not break existing dashboard endpoints or tests.
- Build-free extension (plain JS/HTML/CSS), loaded unpacked.

## Decisions (from brainstorming)
- **Engine:** JobPilot backend + Ollama.
- **Field-mapping strategy:** deterministic-first, LLM-for-the-rest.
- **Sites:** Greenhouse, Lever, Ashby (React-safe) + generic label-matcher fallback.
- **Essays:** fill standard fields + LLM-drafted essays/short-answers (capped; fallback templates).
- **Résumé routing:** auto-detect AI vs BT (archetype classifier), with manual override in the popup.

## Architecture — two units in `job-search-pipeline/`

### Unit A — Backend autofill API
**Files**
- `agents/autofill_mapper.py` (NEW) — pure functions: deterministic field→value mapping + plan assembly. No I/O; fully unit-testable.
- `server/autofill.py` (NEW) — FastAPI `APIRouter` with the endpoints; wires mapper + profile loader + ranker classifier + Ollama.
- `server/dashboard.py` (MODIFY) — `include_router(autofill.router)`; add `CORSMiddleware` (allow `chrome-extension://*` + localhost).

**Endpoints**
1. `GET /api/autofill/profile` → `applicant_profile.yaml` as JSON (identity, address, links, work_authorization, experience, salary, referral, eeoc). Cached by the extension for offline standard-fill.
2. `POST /api/autofill/plan`
   - Request: `{ url, job_title?, company?, page_text?, resume_pref?: "auto"|"ai"|"bt", fields: [{ id, label, name, type, options?, required? }] }`
   - Steps: classify archetype (ranker title/keyword classify; honor `resume_pref`) → choose résumé (BT→`base_resume_bt`, else `base_resume`) → deterministic-map standard fields → call Ollama for remaining unmapped/essay/ambiguous fields (profile + résumé summary + field list; cap N essays; timeout → `fallback_essays`/blank).
   - Response: `{ archetype, archetype_label, resume_used, fields: [{ id, value, confidence, source: "deterministic"|"llm"|"fallback", needs_review }], stats: { filled, needs_review, llm_used } }`
3. `GET /api/autofill/health` → `{ ok, ollama_up, profile_loaded }` for the popup status dot.

**Deterministic categories** (label/name regex → value): first/last/full name; email; phone; address city/state/zip/country; LinkedIn; GitHub; portfolio/website; authorized-to-work (yes/no); sponsorship (yes/no); citizenship; years experience; highest education; current employer/title; desired salary; "how did you hear" (referral); EEOC gender/race/veteran/disability/hispanic; school/degree (from résumé). Free-text essays and unknown selects → LLM.

### Unit B — Chrome MV3 extension (`browser-extension/`)
**Files**
- `manifest.json` — MV3; `permissions: [activeTab, scripting, storage]`; `host_permissions: ["http://127.0.0.1:7777/*"]`; action popup.
- `popup.html` / `popup.css` / `popup.js` — Autofill button, résumé selector (Auto/AI/BT, default Auto), backend-status dot, post-run summary ("Filled N · M need review"), dashboard link. Premium dark-glass styling to match the dashboard.
- `background.js` (service worker) — fetch profile (cache in `chrome.storage`), POST plan, orchestrate inject→scan→fill; bridges localhost fetches via host_permissions.
- `content/scan.js` (injected) — scan DOM for fillable controls; resolve labels (`<label for>`, `aria-label`/`aria-labelledby`, placeholder, preceding text, `name`); tag each control `data-jpaf-id="f{n}"`; return normalized list `{id,label,name,type,options,required}`.
- `content/fill.js` (injected) — apply plan: React/Vue-safe value set, radios/checkboxes, selects by best option-text match; outline filled (green) vs needs-review (amber); in-page toast; detect file inputs → flag; **never submit**.

**Data flow:** popup click → background ensures profile (cache or GET) → `executeScript` scan.js → fields → POST `/api/autofill/plan` → `executeScript` fill.js(plan) → result → popup summary.

## React/Vue-safe value setting
```js
const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
el.dispatchEvent(new Event("input",  { bubbles: true }));
el.dispatchEvent(new Event("change", { bubbles: true }));
el.blur();
```
Selects: set value then dispatch `change`; if no exact option, choose closest by normalized text. Radios/checkboxes: match by value/adjacent label, then `click()`.

## Error handling
- **Backend unreachable** → popup: "JobPilot offline — filling standard fields from cache"; background uses cached profile + a JS deterministic subset (mirrors backend categories); essays skipped.
- **Ollama down** → backend returns deterministic + `fallback_essays` (source `fallback`).
- **No fillable fields** → toast "No application fields detected here."
- Every field fill wrapped in try/catch; failures counted, never break the page.
- **File input present** → flagged `needs_review` with a hint to attach manually.

## Security / privacy / ToS
- On-demand only — no persistent auto-running content script.
- No submit click, no CAPTCHA handling, no navigation.
- Data local only; no network calls except `127.0.0.1:7777`.
- host_permissions limited to the local backend; injection via `activeTab` on user action.

## Testing
**Backend (pytest)**
- `test_autofill_mapper.py` — deterministic mapping per category; archetype→résumé selection; plan assembly with a fake Ollama (monkeypatch) for essays + timeout fallback.
- `test_autofill_api.py` — endpoint contracts (profile shape, plan request/response, health) via FastAPI `TestClient`, Ollama mocked.

**Extension (Playwright, Python, extension loaded)**
- `tests/e2e/fixtures/sample_forms.html` — Greenhouse-like, Lever-like, and generic field sets (name/email/phone/links/work-auth/EEOC/essay/select/file).
- `tests/e2e/test_extension_autofill.py` — launch Chromium persistent context with `--disable-extensions-except` + `--load-extension`; open the fixture; run scan+fill against the live backend (or a stub); assert standard fields filled, essay filled, select chosen, file input flagged, and **no submit navigation** occurred.
- `scripts/verify_autofill.py` — prints a `[Loop]`-style line (fields scanned/filled/needs-review), consistent with the dashboard QA loop.

> Playwright drives MV3 extensions only in a Chromium **persistent context**; the toolbar popup is awkward to click headlessly, so tests trigger the scan/fill logic via `executeScript`/page calls against the fixture rather than literally clicking the toolbar icon. The plan specifies exact steps.

## Repo location & install
- Location: `job-search-pipeline/browser-extension/`.
- Install: `chrome://extensions` → Developer Mode → **Load unpacked** → select `browser-extension/`. Requires the JobPilot backend running (auto-starts at login). README section to be added.
