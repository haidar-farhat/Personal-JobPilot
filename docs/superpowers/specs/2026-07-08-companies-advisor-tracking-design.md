# Companies Section, Application Trees & Advisor Tracking — Design

**Date:** 2026-07-08
**Status:** Approved (Approach A — DB-backed Companies + Advisor tabs)
**Origin:** Matthew's request after receiving job leads from Bianca (Vantage Point career counselor): (1) a Companies section with detailed company profiles tailored to him, (2) per-company trees of positions applied for, (3) reliable application tracking presentable at Vantage Point meetings.

## Decisions made during brainstorming

| Question | Decision |
|---|---|
| Advisor report format | **Both** — dashboard tab AND print-to-PDF export |
| Company profile lifecycle | **Seeded + LLM-refreshed** — seeded from 2026-07-08 research, local LLM drafts new/refreshed profiles, hand-editable |
| Tree scope | **Full pipeline** — all stages per company, applied branch highlighted |
| Tracking gaps to fix | All three: external-apply capture, status history, richer per-application fields |

## Goals

1. Every application Matthew makes — via the bot, the extension, or externally — is recorded with source and timestamp.
2. Status changes are historized so "what happened since my last Vantage Point meeting" is answerable.
3. A Companies tab presents tailored company profiles with full-pipeline application trees.
4. An Advisor tab renders a since-date report, printable to clean PDF, with zero new dependencies.

## Non-goals

- Email/inbox scanning to auto-detect responses (future idea, out of scope).
- Google-Sheet sharing for Bianca (Approach C — possible later complement).
- Scheduler-driven auto-refresh of company profiles (refresh is manual-trigger only).
- Changing scanner behavior or target_companies.yaml semantics (companies link by name, not config merge).

## 1. Data model — migration `db/migrations/006_add_companies_and_events.py`

### New table `companies`

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | |
| name | String(300) unique, not null | canonical display name |
| name_normalized | String(300) indexed | via shared normalizer (below) |
| careers_url | String(2000) | |
| ats_platform | String(50) | greenhouse / ashby / lever / workday / radancy / custom |
| priority | String(20) | high / medium / low |
| status | String(20) | target / watch / paused |
| overview_md | Text | "what they do" — LLM/seed-owned |
| why_fit_md | Text | "why they fit Matthew" — LLM/seed-owned |
| hiring_bar_md | Text | levels, YOE bars, comp bands, gotchas — LLM/seed-owned |
| notes_md | Text | **user-owned; never written by LLM or seed-refresh** |
| profile_source | String(20) | seeded / llm / manual |
| profile_backup | JSON nullable | previous profile snapshot, written just before an LLM refresh overwrite (one-step undo) |
| draft_status | String(20) nullable | drafting / failed / null=idle — persisted so the card state survives restarts |
| suggested | Boolean default false | true for companies Claude added unprompted |
| last_refreshed_at | DateTime nullable | |
| created_at | DateTime | |

### New table `application_events`

| Column | Type | Notes |
|---|---|---|
| id | Integer PK | |
| application_id | Integer FK applications.id, indexed | |
| occurred_at | DateTime not null | |
| from_status | String(50) nullable | null for creation/backfill events |
| to_status | String(50) not null | |
| note | Text nullable | |
| source | String(30) not null | dashboard / extension / auto_applier / review_ui / quick_add / backfill |

### Column additions

- `applications.lead_source` — String(100) nullable (e.g. "Bianca / Vantage Point", "scanner", "extension").
- `applications.next_action` — Text nullable.
- `jobs.company_id` — Integer FK companies.id, nullable, indexed.

### Backfill (in the same migration)

- For each existing Application: synthesize events from existing date fields (`date_applied` → applied, `response_date` → response_received, `interview_date` → interview) with `source="backfill"`, plus one event for current status if not covered. Backfilled timestamps are approximations and the UI labels them.
- Populate `jobs.company_id` by matching `normalize_company_name(jobs.company)` against seeded companies.

### Company-name normalizer

`utils/company_names.py` → `normalize_company_name(s)`: casefold, strip punctuation, strip legal/common suffixes ("inc", "llc", "corp", "financial", "technologies", "global"…). Used by the migration backfill, scanner ingest, job_import, and the Companies tab rollup. Unit-tested against real variants seen in the DB ("Chime Financial, Inc" → "chime").

## 2. Status-event capture

New helper in `db/database.py`:

```python
def record_status_change(session, application, new_status, *, source, note=None) -> None
```

Sets `application.status`, stamps the matching date field (applied/response/interview — preserving the logic currently inlined in dashboard.py), and appends an `ApplicationEvent`. All writes to `Application.status` go through it. Known write-sites to convert (verified 2026-07-08):

1. `server/dashboard.py` `api_update_status` (POST /api/application/{id}/status) — source="dashboard"
2. `agents/auto_applier/runner.py:175` — source="auto_applier"
3. `ui/review_app.py:155` (`update_application_status` helper + its three call sites) — source="review_ui"
4. All new endpoints below.

A grep-guard test asserts no direct `.status =` assignment on Application outside `record_status_change` (pattern-based, excluding the helper itself).

## 3. Applied-capture endpoints (`server/applied.py`, router mounted like job_import)

### POST `/api/applied/record`

Body: `{url, title?, company?, lead_source?, source: "extension"|"dashboard"}`.
Behavior: match existing Job by exact `url`, then by `dedup_hash` of (title, company) when provided; if no match, create Job+Application via the `job_import` machinery (`source="applied_manual"`, scoring deferred); then `record_status_change(..., APPLIED, source=...)` and set `lead_source`. **Idempotent:** if the application is already APPLIED or beyond (response/interview/offer stages), return `{already: true}` and change nothing — never regress a status.

### Extension: "Mark applied ✓" widget button

One button in the existing widget (`browser-extension/content/widget.js`), enabled after a fill or on any recognized ATS page. Sends page URL + `document.title` (+ company when the wizard engines know it) via the service worker to `/api/applied/record`. Follows widget conventions: isolated-world handlers, rebuild-safe, no clobbering. Feedback states: ✓ recorded / already recorded / backend unreachable (retry).

### Dashboard quick-add

Small form in the Applications tab header: URL field + lead-source select (defaults to last used, persisted in localStorage; free-text option). Calls the same endpoint with `source="dashboard"`. Unreachable/unparseable URL still creates a minimal record (title/company prompted inline, editable later) — a lead from Bianca must never be droppable.

## 4. Companies API + tab

### Endpoints (`server/companies.py`)

- `GET /api/companies` — all companies + per-stage rollup counts (watching=FOUND/SCORED/MATERIALS_READY/QUEUED/APPROVED, applied=APPLIED, in_play=RESPONSE_RECEIVED/INTERVIEW, closed=REJECTED/NO_RESPONSE/NO_LONGER_AVAILABLE/SKIPPED), last-activity timestamp, plus an "Other companies" rollup for jobs with `company_id IS NULL` grouped by normalized name.
- `GET /api/company/{id}` — full profile + pipeline tree: stage groups → job rows (id, title, fit_score, status, date_found, date_applied, url, lead_source) → per-application event history.
- `POST /api/companies` — `{name, careers_url?}`; creates the row with `draft_status="drafting"` and kicks off the background LLM draft (§5); when the draft lands, profile fields fill and `profile_source` becomes `"llm"`. Returns 202-style `{id, draft_status: "drafting"}`.
- `PATCH /api/company/{id}` — edit any profile/metadata field; sets `profile_source="manual"` when a profile field is edited by hand.
- `POST /api/company/{id}/refresh` — re-draft via LLM (§5).
- `DELETE /api/company/{id}` — removes the company (jobs keep rows; `company_id` nulled).

### UI (in the existing single-file `server/static/index.html` dashboard, matching its vanilla-JS tab pattern)

- **Card grid:** name, priority badge, ATS chip, four stage counts, one-line why-fit teaser (first line of `why_fit_md`), "suggested" badge where applicable, drafting/failed state.
- **Detail view:** profile sections (overview / why-fit / hiring bar / notes) with inline edit; then the **pipeline tree** — stage groups as expandable nodes, applied branch visually highlighted, job rows expand to show the event timeline. Markdown rendered with the dashboard's existing escaping helpers (no new renderer dependency; simple md→HTML for headings/bold/lists/links only).

## 5. LLM profile drafting — `agents/company_profiler.py`

- Input: careers-page text (fetched with the existing scanner fetch utilities; Playwright fallback for JS-rendered pages), company name, and the same resume summary the ranker loads.
- Output: strict JSON `{overview_md, why_fit_md, hiring_bar_md, ats_platform_guess}` via `utils/ollama_client.generate_json` (16k ctx accommodates page + resume).
- Runs as a FastAPI background task; card polls via `GET /api/company/{id}` until `draft_status` clears to null (dashboard already has an SSE/`/api/stream` pattern — reuse if trivial, else poll).
- **Refresh rules:** overwrites `overview_md`/`why_fit_md`/`hiring_bar_md` after a UI confirm; if `profile_source="manual"` the confirm warns explicitly; `notes_md` never touched; the pre-overwrite snapshot is written to `profile_backup` (§1) so one-step undo is possible.
- Failure → `draft_status="failed"` surfaced on the card with retry; fields stay hand-editable.

## 6. Seed data

- `config/companies_seed.yaml` — **gitignored** (add to .gitignore under personal data), one entry per company with the §1 profile fields, written from the 2026-07-08 research (Chime, Affirm, Plaid, Stripe, Coinbase, Ramp, Brex, Intuit/Credit Karma, plus Jack & Jill recorded as `status: watch` platform-entry) + up to ~5 additional suggested companies (researched at implementation time, `suggested: true`).
- `scripts/seed_companies.py` — idempotent upsert by normalized name; never overwrites a company whose `profile_source="manual"`; re-runnable.
- Source research markdown is preserved under `output/research/2026-07-08/` (gitignored output dir) so seeds are auditable.

## 7. Advisor tab + print

- `GET /api/advisor/report?since=YYYY-MM-DD` returns JSON: headline stats (applied / responses / interviews / offers-rejections in range, computed from `application_events`), per-company sections (company name, applications with event timelines in range, lead_source, next_action), and a "new since" list of newly found high-score jobs (optional context block).
- UI: date picker defaulting to the last-used value (localStorage), stat cards, per-company sections. Backfilled events render with an "≈" approximate marker.
- **Print:** a Print button calling `window.print()`; `@media print` stylesheet hides nav/controls, formats sections for paper, adds a header ("Matthew Cromaz — application report, {range}"). Browser print-to-PDF is the export path; no server-side PDF dependency.

## 8. Error handling summary

| Failure | Behavior |
|---|---|
| LLM draft fails / Ollama down | Card shows "draft failed — retry"; manual editing always available |
| Quick-add URL unreachable | Minimal record created; inline prompt for title/company |
| Extension backend unreachable | Button shows retry state; no data loss (URL/title held in widget) |
| Job matches no company | Visible under "Other companies" rollup; can promote to a company in one click (creates company + links) |
| Applied-record on already-applied | No-op `{already: true}`; statuses never regress |
| Report over pre-migration history | Backfilled events labeled approximate |

## 9. Testing

- **Units:** `normalize_company_name` (real DB variants), `record_status_change` (event row + date stamping + regression guard), report aggregation (ranges, backfill labeling), seed idempotency, profiler JSON handling (mock ollama).
- **API contracts** (pattern: `tests/e2e/test_api_contracts.py`): companies CRUD, `/api/applied/record` idempotency + create-path, advisor report shape.
- **Extension:** QA fixture page (pattern: `server/static/qa_greenhouse.html`) exercising the Mark-applied button against a stub endpoint; real-click QA per house rule (synthetic events don't cross worlds).
- **Grep-guard:** no direct `Application.status` writes outside the helper.
- **Manual:** print stylesheet visual check in Chrome print preview.

## 10. Build order

1. **Phase 1 — capture:** migration 006 + normalizer + `record_status_change` conversion + `/api/applied/record` + extension button + dashboard quick-add. *(Tracking is trustworthy from this point on.)*
2. **Phase 2 — companies:** companies API + tab + trees + seed YAML/script.
3. **Phase 3 — profiler:** company_profiler + add/refresh flows.
4. **Phase 4 — advisor:** report endpoint + tab + print stylesheet.

Each phase is independently shippable and testable; later phases only add.
