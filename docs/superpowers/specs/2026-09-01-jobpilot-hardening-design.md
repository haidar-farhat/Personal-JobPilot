# JobPilot hardening — bot reliability, dashboard health, Claude provider

**Date:** 2026-09-01
**Status:** Implemented in the same session (autonomous run; user asked to "make the bot and dashboard as best as you can with Fable 5.1")
**Baseline:** 530 unit tests green; whole stack found stopped (no watchdog / dashboard / scheduler / Ollama process)

## Evidence that drove the scope

| Finding | Source | Cost today |
|---|---|---|
| Auto-apply retries the same login-walled jobs every 25-min cycle (USAA, Vanta), trips the 3-failure halt, so no other candidate is ever attempted | `logs/scheduler.log` | bot effectively never auto-applies |
| 9 of 100 configured ATS boards 404 on every scan (OpenAI, W&B, Netflix, Anyscale, Snowflake, Cohere, Mode, Brex, Cursor) | live probe of `target_companies.yaml` | 6 target companies silently unscanned; log noise |
| `logs/scheduler.log` 1.0M lines, `logs/dashboard.log` 221k lines, no rotation | watchdog opens logs in append mode forever | disk + unreadable logs |
| 964 `ConnectionResetError` tracebacks in dashboard log (SSE client disconnect on Windows Proactor loop) | `logs/dashboard.log` | noise hides real errors |
| 13 dashboard routes are `async def` but do only sync SQLAlchemy work; SSE generator calls sync DB helpers directly | `server/dashboard.py` | every request blocks the event loop; UI stalls during scans |
| Header "Ollama" dot is hard-coded green on page load; nothing shows scheduler liveness or auto-apply blockers | `server/static/index.html` | user cannot tell the pipeline is dead from the dashboard |
| SSE re-fetches the full 2000-row application list every time total changes (every few seconds mid-scan) | `index.html connectSSE` | 4 MB fetch + full re-render loop |
| LLM chain supports Ollama + optional OpenAI only | `utils/ollama_client.py` | no way to use Claude Fable 5.1 |

## Design

### 1. Auto-apply candidate memory (`agents/auto_applier/runner.py`)
`_candidates()` now skips applications whose last attempt failed:
- **Permanent** statuses (login wall, CAPTCHA, honeypot, too complex / too many essays, not-Workday, no resume field) are never retried.
- Any other `failed_*` is retried only after `guardrails.retry_cooldown_hours` (default 24).
`_record_result()` writes a human-readable `next_action` ("Apply manually — bot hit login wall") on permanent failures so the dashboard can surface it. One unit test pins the filter.

### 2. Board slug repair (`config/target_companies.yaml`)
Verified by HTTP probe: OpenAI → Ashby `openai`; Anyscale → Ashby `anyscale`; Snowflake → Ashby `snowflake`; Cohere → Ashby `cohere`; Cursor → Ashby `cursor`; Brex → Greenhouse `brex`. Netflix, Weights & Biases (now CoreWeave), Mode Analytics (now ThoughtSpot) have no public board API and are removed.

### 3. Log hygiene (`watchdog.py`, `server/dashboard.py`)
- Watchdog rotates a service log to `<name>.log.1` when it exceeds 20 MB before (re)spawning.
- Dashboard installs a logging filter that drops the asyncio `ConnectionResetError` disconnect tracebacks and the uvicorn access lines for the two pollers (`/api/stats`, `/api/stream`).

### 4. Dashboard responsiveness (`server/dashboard.py`)
- Sync-only routes become plain `def` so FastAPI runs them in the threadpool.
- The SSE generator runs `_get_stats` / `_get_applications` / `_get_recent_activity` via `asyncio.to_thread`.
- `_get_stats()` gains `last_scan_at`, `last_scan_age_min`, `scan_errors_24h`, `needs_manual_apply`.

### 5. Dashboard health + manual-apply surfacing (`server/static/index.html`)
- Header health strip: Dashboard / Ollama (from `/healthz`, refreshed every 60 s) / Scanner ("12m ago"; red after 90 min).
- Cards and the detail pane show a "Needs manual apply" pill with the bot's reason when `auto_apply_status` is a permanent failure; Insights gets a "Manual apply" stat.
- SSE application re-fetch is debounced to once per 15 s.

### 6. Claude provider (`utils/anthropic_client.py`, `utils/ollama_client.py`)
Opt-in, off by default, key only from `ANTHROPIC_API_KEY` (never in settings):
```yaml
llm:
  provider: "ollama"            # or "openai" / "anthropic"
  allow_openai_fallback: false
  allow_anthropic_fallback: false
  anthropic_model: "claude-fable-5-1"
  anthropic_effort: "medium"    # low | medium | high | xhigh | max
```
Official `anthropic` SDK, server-side refusal fallbacks enabled, `refusal` stop reason raised as an error so the Ollama chain catches it. JSON mode is instruction + brace extraction (no prefill on Fable). Existing OpenAI semantics unchanged; tests extended.

## Out of scope (deliberately)
Interview story bank, deep company research, nightly integrity pass (backlog items 4, 6, 7) — larger features, not reliability. Committing: left to the user (717 lines of prior uncommitted work plus this session's changes).

## Verification
- `pytest --ignore=tests/e2e` green
- `-m live` e2e suite against a dev instance on :7799
- Playwright screenshots of every view before/after
- Stack restarted through the watchdog; `/api/stats` on :7777 healthy

## Auto-apply engine v2 — 2026-09-07

94 attempts, 0 submissions: the per-ATS Playwright scripts (`ashby.py`, `lever.py`, `greenhouse.py`, `generic.py`) hard-coded aria-labels that no longer exist (`skip_field_absent` on every identity field, then a Submit click that validation blocked), and Greenhouse's new `job-boards.greenhouse.io` DOM hid the résumé input behind an "Attach" button (`failed_resume_field_not_found` ×22).

### Design
`agents/auto_applier/mapper_engine.py` — `MapperApplier(BaseAutoApplier)`, one engine for every form-based ATS. It reuses the Chrome extension's autofill stack instead of maintaining selectors: injects `browser-extension/content/scan.js` + `fill.js` (`bypass_csp=True` on the Playwright context), asks the live dashboard `POST /api/autofill/plan` for the plan, applies it with `window.__jpafApply`, and attaches files itself with `set_input_files` (fill.js can't — no service worker). Order: navigate (Ashby `/application`, Lever `/apply`, Greenhouse `iframe#grnhse_iframe` src, else click an Apply control) → bail on CAPTCHA / login wall → attach résumé (+ cover letter) first, since Ashby/Greenhouse parse it and re-render → scan → plan → fill, then one more pass for conditional fields → required-field audit → submit → verify. Never `networkidle`; `domcontentloaded` + short settles.

Engine-side fixes the extension's scan doesn't cover (marked `ponytail:` in code, candidates for `scan.js`):
- Lever custom-question cards: question text lives in `.application-question > .application-label`; scan.js reported the card title ("USA CORP"). Radio/checkbox groups get their `options` from the group's labels.
- react-select comboboxes (all Greenhouse yes/no questions) expose no `<option>`s; the engine opens each once and harvests the `aria-controls` listbox (≤ 40 options) so the planner binds answers to a real choice. Country/city autocompletes (hundreds of options) are skipped — fill.js types into those.
- Emptiness check reads the react-select control (`single-value` / `has-value`), not the always-empty input.
- `has_captcha` (base.py) now ignores the invisible reCAPTCHA badge (every Greenhouse board) and hCaptcha's passive enclave frame (Lever). A real challenge appearing after Submit is still caught inside the verify loop.

New outcomes, both permanent in `runner._skip_after_failure` (`NEVER_RETRY`): `failed_required_fields_unfilled` (labels in the message; dashboard "Needs manual apply") and `submitted_unverified` (Submit clicked, no confirmation in 20 s — never retried so we can't double-apply; `next_action` tells the user to check email; counts against the daily quota). Guardrails kept: `max_essays_per_app` / `bail_on_required_essay` count only free-text LLM answers (an LLM picking "Yes" from harvested options is not an essay).

Routing: `APPLIER_MAP` sends greenhouse / ashby / lever / generic / custom (and any unknown prefix, e.g. SmartRecruiters) to `MapperApplier`; Workday keeps `WorkdayAutoApplier`. The old per-ATS modules stay on disk only because `tests/test_apply_dry_run.py` / `tests/test_auto_applier_units.py` import them; nothing routes to them.

### Verification
- `pytest tests --ignore=tests/e2e`: 577 passed (dispatch tests re-pointed at `MapperApplier`; `test_mapper_engine_outcomes_are_permanent` added to `tests/test_auto_apply_retry_memory.py`).
- Live dry-run (`dry_run=True`, headless, real URLs, no DB writes, nothing submitted), 2026-09-07:

| app | source | URL | outcome | detail |
|---|---|---|---|---|
| 1885 | ashby:ramp | jobs.ashbyhq.com/ramp/196e4e25-… | **dry_run — would submit** | 8 fields, filled 5, review 1 (optional payroll-location combo), résumé attached via hidden dropzone input, 0 required empty, 9 s |
| 1895 | greenhouse:checkr | job-boards.greenhouse.io/checkr/jobs/8174046 | **dry_run — would submit** | 15 fields, filled 14, review 3, 2 LLM-picked yes/no answers, résumé attached via hidden "Attach" input, 0 required empty, 35 s. Was `failed_captcha` (invisible badge) before the `has_captcha` fix |
| 1499 | lever:cologix | jobs.lever.co/cologix/022417e1-… | **dry_run — would submit** | 16 fields, filled 13, review 2 (optional portfolio URL, veteran status), résumé attached, 0 required empty, 8 s. Was `failed_required_fields_unfilled` (work-auth / sponsorship / home address / salary cards) before the Lever label fix |
| 1884 | greenhouse:doximity | job-boards.greenhouse.io/doximity/jobs/8024477 | failed_required_fields_unfilled | résumé + cover letter attached, 21 fields incl. 1 conditional, filled 16; "Which work authorization best describes you" combo left unbound → correctly handed to manual apply, 72 s |
| 1834 | ashby:vanta | jobs.ashbyhq.com/vanta/0fab2072-… | failed_required_fields_unfilled | filled 10/12; "Current/Most Recent Company Name" unmapped by the planner → manual apply (was `failed_confirmation_check` under the old bot, i.e. it submitted blind) |

Known gaps (planner-side, `agents/autofill_mapper.py`): multi-option work-authorization questions and "Current/Most Recent Company Name" don't map deterministically. Not exercised live: real Submit + confirmation detection (dry-run only, by design); embedded Greenhouse boards (`?gh_jid=`) — navigation snippet carried over from the old applier.
