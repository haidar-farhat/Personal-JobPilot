# JobPilot — AI-Role Targeting + Indeed-Style UI — Design Spec

- **Date:** 2026-06-29
- **Status:** Proposed (awaiting user review)
- **Author:** Claude (with Matthew Cromaz)
- **Source resume:** `C:\Users\matth\Downloads\rezumax (1).pdf`

## 1. Goals

1. Make JobPilot target **AI-forward roles** — AI Engineer, AI/Solutions/Forward-Deployed Engineer, Prompt Engineer, AI Analyst — alongside the existing analyst/DS archetypes, driven by the new AI-engineering-focused resume.
2. Prefer roles that **use AI tooling heavily** ("as much AI as possible during the role"), even when the title isn't an AI title.
3. Re-skin the dashboard to **Indeed's visual language** with a **full faceted filter rail** (role category + location + date + status + fit + AI-forward).
4. Everything **QA-verified via Playwright**, then kept honest by a **continuous QA loop**.

## 2. Non-goals (YAGNI)

- No LinkedIn / Indeed / Glassdoor scrapers. Keep the existing Greenhouse/Lever/Ashby + career-page scanners (user-confirmed). The QA loop's references to those boards are reinterpreted against the real ATS sources.
- No pixel-perfect clone of Indeed's actual codebase — Indeed returns HTTP 403 to fetches. We reproduce its **design language**, adapted to JobPilot's data.
- The QA loop never submits a real application and never weakens production auto-apply guardrails.

## 3. Confirmed decisions

| # | Decision | Choice |
|---|---|---|
| D1 | AI-forward intensity signal | **Include** (new `JobScore` column + migration + ranker change) |
| D2 | Existing 174 jobs | **Backfill re-score** into new archetypes (+ ai_intensity) |
| D3 | UI scope | **Full Indeed rebuild** (reuse existing JS/SSE data layer) |
| D4 | Job sources | **Keep current ATS** (Greenhouse/Lever/Ashby + career pages) |
| D5 | Filters | **Full faceted rail** (category + location + date + status + fit + AI-forward) |
| D6 | BT job sources | **ABA providers' career pages + school districts via EdJoin** |
| D7 | BT apply mode | **Auto-submit where ATS supported + guardrails pass; else queue tailored materials for one-click manual apply** |

> **Two tracks.** Track A = AI/tech roles (§4–§10). Track B = part-time Behavioral Technician roles (§14). Same pipeline, different archetype + comp logic.

## 4. Archetype taxonomy

Edit `config/archetypes.yaml`. Three **new** archetypes; existing six kept (with `ml_engineer` re-scoped). The ranker (`agents/ranker.py`) builds its LLM classification list from this file and validates each weight vector sums to 1.0, so this is primarily a config change. We will verify `ARCHETYPE_PROMPT_TEMPLATE` enumerates archetypes + descriptions from the YAML (it does today) and that `dashboard.py::_archetype_label` resolves new keys.

### 4.1 New archetypes

**`ai_engineer`** — build LLM/GenAI/agentic applications (Matthew's new headline strength).
- keywords: `ai engineer`, `a.i. engineer`, `llm engineer`, `generative ai engineer`, `genai engineer`, `applied ai engineer`, `applied ai`, `prompt engineer`, `agent engineer`, `ai developer`, `llm developer`, `ai software engineer`
- auto_apply_min_score: **76**
- weights (sum 1.00): technical_fit 0.18, level_match 0.15, comp_range 0.08, location_remote 0.08, archetype_fit 0.16, skills_overlap 0.15, growth_signal 0.06, company_reputation 0.05, ats_keyword_density 0.04, gap_severity 0.05

**`ai_solutions_engineer`** — customer-facing technical AI roles (fits freelance SWE + consulting + client decks).
- keywords: `solutions engineer`, `forward deployed engineer`, `forward-deployed`, `fde`, `ai solutions engineer`, `ai solutions architect`, `solutions architect`, `sales engineer`, `customer engineer`, `implementation engineer`, `deployment engineer`, `field engineer`
- auto_apply_min_score: **74**
- weights (sum 1.00): technical_fit 0.16, level_match 0.14, comp_range 0.08, location_remote 0.08, archetype_fit 0.16, skills_overlap 0.14, growth_signal 0.08, company_reputation 0.06, ats_keyword_density 0.05, gap_severity 0.05

**`ai_analyst`** — AI-augmented analytics / "AI analyst" (bridges analyst background + AI tooling).
- keywords: `ai analyst`, `ai data analyst`, `generative ai analyst`, `ai operations analyst`, `ai research analyst`, `analytics engineer (ai)`
- auto_apply_min_score: **72**
- weights (sum 1.00): technical_fit 0.16, level_match 0.17, comp_range 0.09, location_remote 0.09, archetype_fit 0.13, skills_overlap 0.13, growth_signal 0.06, company_reputation 0.05, ats_keyword_density 0.06, gap_severity 0.06

### 4.2 Existing archetypes
- `ml_engineer`: **re-scope to production ML / MLOps**. Remove `ai engineer` / `applied ml engineer`-as-AI keywords that now belong to `ai_engineer`; keep `machine learning engineer`, `ml engineer`, `mlops engineer`, `ml platform engineer`, `model deployment`. Weights unchanged.
- `data_analyst`, `data_scientist`, `quantitative_analyst`, `business_analyst`, `product_analyst`, `unknown`: unchanged.

### 4.3 Keyword + company expansion
- `config/settings.yaml` → `search.keywords`: append `AI Engineer`, `LLM Engineer`, `Generative AI Engineer`, `Prompt Engineer`, `Applied AI Engineer`, `Solutions Engineer`, `Forward Deployed Engineer`, `AI Solutions Engineer`, `Sales Engineer`, `AI Analyst`, `Machine Learning Engineer`. (Keep existing analyst/DS terms.)
- `config/target_companies.yaml` → add AI-forward employers with public ATS boards (verify each board slug during implementation): Anthropic, OpenAI, Scale AI, Databricks, Hugging Face, Perplexity, Cohere, Mistral, Together AI, Glean, Sierra, Harvey, Cresta, Decagon, Runway. Skip any without a Greenhouse/Lever/Ashby board.

## 5. AI-intensity signal (D1)

Captures "uses as much AI tooling as possible" as a cross-cutting score, independent of the weighted archetype rubric (so we don't have to re-balance every weight vector).

- **Data model:** add `ai_intensity` (Integer 0–100, nullable) and `ai_tools` (JSON list, nullable) to `JobScore` in `db/models.py`. New migration `db/migrations/004_add_ai_intensity.py` mirroring the existing migration style (`001`–`003`).
- **Ranker:** in `agents/ranker.py` stage-2 scoring (or a small dedicated stage), have Gemma also return `ai_intensity` (0–100: how central is building-with / using AI tools to the day-to-day) and `ai_tools` (e.g. `["LLM APIs","RAG","Copilot","agents"]`). Deterministic Python keeps the weighted fit score unchanged; `ai_intensity` is stored alongside.
- **Surfacing:** `_serialize_application` adds `ai_intensity` + `ai_tools`. UI shows an **"⚡ AI-forward"** badge when `ai_intensity >= 60` (threshold configurable in `settings.yaml`), exposes an **AI-forward filter facet**, and offers **"Sort: AI-forward"** (secondary sort by `ai_intensity`, then fit score).
- **Guard:** all reads use `getattr(score, "ai_intensity", None)` so pre-backfill rows render cleanly.

## 6. Resume & profile sync

Align config with `rezumax (1).pdf` so skills-overlap scoring and generated `.docx` materials reflect the AI-engineering positioning.

**`config/base_resume.yaml`:**
- `technical_skills.Languages`: add **TypeScript**.
- `technical_skills."Tools & Infrastructure"` (or rename to "Web, Cloud & Tools"): add **AWS (EC2/Lightsail), Vercel, Nuxt/Vue, Node.js, WordPress, PostgreSQL, REST & serverless APIs**.
- `project_experience`: add **Leasing Agent 415 — Full-Stack Revival & Migration** (Nuxt/Vue, AWS→Vercel, Node.js, Next.js, Postgres) and **ForgetMeNote — iOS Port** (Swift/SwiftUI, SwiftData, Apple Vision, Google ML Kit, Maestro). Keep trading system + JobPilot; portfolio optional.
- `work_experience`: add **Freelance Software Engineer | Leasing Agent 415 (Compass)**, San Francisco, June 2026–Present (lead bullet). Keep Rithum, BIA, Investment Analyst.
- `certifications`: add **DataCamp AI Engineer for Developers Associate (2026)** and **DataCamp Data Analyst Associate (2026)**.
- `strong_match_keywords`: add `AI engineering`, `MCP`, `prompt engineering`, `RAG`, `agents`, `Claude API`, `OpenAI API`, `Ollama`, `full-stack`, `TypeScript`, `solutions engineering`, `forward deployed`.

**`config/applicant_profile.yaml`:**
- `links.github`: `https://github.com/TheCromazone`.
- Add one AI-engineering-flavored option to `fallback_essays` (keep analyst version too).
- **Do not** change `guardrails.dry_run` (stays `false`); **do not** raise salary or work-auth fields. `experience.total_years` stays entry-level.

## 7. Indeed-style UI rebuild (D3)

Rebuild `server/static/index.html`. **Back up first** to `server/static/index.html.bak-pre-indeed` (matches existing `.bak` convention). Preserve every current capability by reusing the existing JS data layer: `fetchJSON`, `/api/stats`, `/api/applications?limit=500`, `/api/activity`, `/api/stream` (SSE live updates), `/api/application/{id}/evaluation`, status/notes POSTs, file links, and the Insights analytics.

### 7.1 Design tokens
- Font: `"Noto Sans", "Helvetica Neue", Helvetica, Arial, "Liberation Sans", Roboto, sans-serif`.
- Colors: primary/links `#2557A7`, hover/active `#164081`, text `#2D2D2D`, meta `#595959`, subtle `#767676`, border `#D4D2D0`, divider `#E4E2E0`, page bg `#FFFFFF`, panel bg `#F3F2F1`, success/applied `#0A7E07`, warning `#B45309`. Card radius 8px, button radius 8px, filter-pill radius 999px, subtle hover shadow `0 1px 6px rgba(0,0,0,.12)`.
- Fit-score chip colors: Strong ≥75 green, Maybe 60–74 amber, Stretch <60 grey.

### 7.2 Layout (top → bottom)
1. **Header** (white, ~60px): "JobPilot" wordmark (Indeed-style blue), live status dot (dashboard/Ollama/scheduler health), last-update time.
2. **Search bar**: two fields in a rounded, shadowed container — **What** (job title / company; magnifier icon) + **Where** (location / "Remote"; pin icon) — divider — blue **Find** button. Drives client-side filter.
3. **Filter rail** (left, sticky) — see §8.
4. **Results list** (center): job cards, count header ("174 jobs"), sort dropdown (Relevance/Fit/AI-forward/Date).
5. **Detail pane** (right, sticky): replaces today's drawer. Renders selected job: title, company, location, salary, fit chip, archetype badge, ⚡AI-forward badge, status control, apply/open-link + résumé/cover `.docx` buttons, the 10-dimension score grid, and the collapsible evaluation report (from `/api/application/{id}/evaluation`).
6. **Insights**: retained as a secondary tab/section.
- Responsive: rail collapses to a "Filters" button + list stacks above detail on narrow widths.

### 7.3 Job card anatomy
Title (blue, 600) · company · location (+ "Remote" tag) · salary if present · fit-score chip · archetype badge · ⚡AI-forward badge (if intensity ≥ threshold) · status pill · "Posted Nd ago" (from `date_found`). Selected card gets a left blue border + tinted bg.

## 8. Faceted filter rail (D5) — client-side

All facets compute over the already-loaded `/api/applications` payload (no new query API). Multi-select within a facet = OR; across facets = AND. Each facet shows live counts; a "Clear all" resets. Active filters also reflect to the URL query string for shareable/bookmarkable state.

| Facet | Source field | Options |
|---|---|---|
| Role category | `archetype` / `archetype_label` | one chip per archetype present in data |
| Fit tier | `fit_score` | Strong ≥75 · Maybe 60–74 · Stretch <60 |
| AI-forward | `ai_intensity` | toggle: only `ai_intensity ≥ threshold` |
| Location / Remote | `location`, `is_remote` | Remote · each distinct metro present |
| Date posted | `date_found` / `age_hours` | 24h · 3d · 7d · 14d · any |
| Source | `source` | greenhouse · lever · ashby · career page |
| Status | `status` | one per pipeline stage |
| Min fit score | `fit_score` | slider 0–100 |

The existing "What"/"Where" search inputs feed the same client-side predicate (title/company contains; location contains / Remote).

## 9. Backfill re-score (D2)

One-off script `scripts/rescore_archetypes.py` (new): iterate all `Job`s with a `JobScore`, re-run `classify_archetype` + dimension scoring (+ `ai_intensity`) using the updated config, update each `JobScore` in place inside a transaction, log progress, and be safely re-runnable (idempotent; `--limit`/`--ids` flags for testing). Run with the venv Python against local Gemma. Expected: a few minutes for 174 jobs. The live scheduler keeps running; the script only writes `JobScore` rows.

## 10. QA & verification

### 10.1 Playwright E2E (extend `tests/e2e/`)
Python Playwright via venv `pytest` (the repo's existing harness; `playwright install chromium` ensured). New/updated tests:
- `test_search_what_where`: typing in What/Where filters the list correctly; result count updates.
- `test_facets`: each facet filters correctly; multi-select OR / cross-facet AND; counts accurate; "Clear all" resets; unqualified jobs excluded (no false positives).
- `test_list_detail`: clicking a card selects it and the detail pane renders matching title/score grid/evaluation.
- `test_ai_forward`: ⚡ badge appears iff `ai_intensity ≥ threshold`; AI-forward facet narrows to those.
- `test_apply_simulation`: drives the apply flow through the **`dry_run=True`** path; asserts it reaches `dry_run` / `dry_run_skip_submit` and **never clicks a real Submit**.
- Keep existing API-contract / SSE / render tests green.
- QA screenshots written to `tests/qa-screenshots/` (existing convention).

### 10.2 Continuous QA loop (the `/loop`)
Runs **after** the build and a green E2E pass. Invoked via the loop skill, self-paced. Adapted to the real architecture and made safe:
1. **Check state** — profile/resume/cover configs load; dashboard `:7777`, Ollama `:11434`, scheduler all healthy.
2. **Execute search** — exercise a rotating test keyword (e.g. "AI Engineer", "Solutions Engineer", "Data Analyst") through the dashboard search; record raw count. (Live ATS scan is scheduler-driven; the loop reads current DB state rather than hammering boards.)
3. **Apply filters** — apply facet combos; confirm unqualified excluded, no false positives in the filtered set.
4. **Simulate application** — pick top filtered job; verify résumé/cover present; run the applier in **`dry_run=True`**, stopping before Submit. Never submits; never edits global guardrails.
5. **Log & audit** — pass/fail of parse + form-fill; per-step response times; flag UI/field/extraction errors.
6. **Cooldown** — randomized 5–10s; loop with next keyword/filter config.

**Output line (exact):**
`[Loop #] | Status: SUCCESS/FAILED | Search Term: X | Jobs Scanned: N | Filtered: M | App Simulation: PASS/FAIL | Error Log: None or <error>`

**Safety invariants:** dry-run only; no real submissions; `applicant_profile.yaml guardrails.dry_run` stays `false` for production while the loop passes `dry_run=True` explicitly; no destructive DB writes.

## 11. Sequencing / milestones

1. **Config & resume** — archetypes.yaml (+weights), settings.yaml keywords, target_companies.yaml, base_resume.yaml, applicant_profile.yaml. Unit tests for weight-sum + classification.
2. **AI-intensity** — model field + migration 004 + ranker change + serializer; tests.
3. **Backfill** — `rescore_archetypes.py`; run it.
4. **UI rebuild** — Indeed shell + cards + detail pane + facets (reusing JS/SSE); back up old index.html.
5. **E2E** — extend Playwright suite; get green; capture screenshots.
6. **Continuous QA loop** — engage the loop; confirm steady SUCCESS lines.

## 12. Risks & mitigations

- **UI regression** (index.html is a 117KB single file with SSE/insights). → Back it up; reuse the existing JS data layer; cover with E2E before/after.
- **Re-balanced/incorrect weights** (must sum to 1.0). → Ranker validates on load; a unit test asserts every archetype sums to 1.0.
- **Backfill cost / interruption.** → Idempotent, resumable, `--limit` for a dry test first.
- **Migration safety.** → Additive nullable columns; back up `jobpilot.db` before running migration 004.
- **Target-company board slugs may not exist.** → Verify each AI company has a real Greenhouse/Lever/Ashby board before adding; skip otherwise.
- **Loop safety.** → dry-run only; never submit; never weaken guardrails.

## 13. Acceptance criteria

- New AI archetypes classify correctly on sample AI JDs; weight vectors valid.
- Existing 174 jobs re-scored; AI roles show AI archetypes + ⚡AI-forward where intensity ≥ threshold.
- Dashboard visibly Indeed-style: two-field search, left facet rail, list + detail pane; all prior functionality (SSE live updates, score grid, evaluation, résumé/cover download, status changes, insights) intact.
- Full faceted filtering works (category/location/date/status/fit/AI-forward) with accurate counts and no false positives.
- Playwright E2E green, including the dry-run apply simulation that stops before Submit.
- Continuous QA loop emits the specified line format with steady SUCCESS and no real submissions.

## 14. Track B — Behavioral Technician (BT) jobs

A second, parallel track for **part-time Behavioral Technician / ABA** roles in the Bay Area (SF-preferred) paying **≥ $30/hr**. Motivation: Matthew is a part-time BT at BIA San Mateo earning **$24/hr** while a peer earns **$34/hr via SFUSD** for comparable work, and BIA isn't offering a permanent schedule. Same pipeline (scan → score → tailor → apply/queue → dashboard), distinguished by a new archetype + hourly-comp logic.

### 14.1 New archetype `behavioral_technician`
- label: "Behavioral Technician (ABA)"
- keywords: `behavioral technician`, `behavior technician`, `registered behavior technician`, `rbt`, `aba therapist`, `behavior interventionist`, `behavior therapist`, `applied behavior analysis`, `behavioral health technician`, `1:1 aba`, `paraprofessional` (behavior)
- auto_apply_min_score: **70**
- weights (sum 1.00): technical_fit 0.06, level_match 0.10, comp_range **0.20**, location_remote 0.16, archetype_fit 0.16, skills_overlap 0.14, growth_signal 0.04, company_reputation 0.04, ats_keyword_density 0.04, gap_severity 0.06
- rationale: **pay and location dominate**; technical_fit is largely irrelevant for ABA.

### 14.2 Hourly pay handling + $30/hr floor
- Parse hourly rates from title/`salary_text`/JD (`$30/hr`, `$28–$35 an hour`, `$30.00 hourly`). Add `pay_period` (hourly|annual|unknown) + normalized `hourly_min`/`hourly_max` to `Job` (additive nullable **migration 005**) for filter/display.
- `config/settings.yaml`: `comp.min_hourly_by_archetype.behavioral_technician: 30`.
- **Hard guardrail:** auto-applier never submits a BT role whose parsed hourly rate is known < $30/hr (skip; queue only if pay undisclosed, flagged "pay unconfirmed").
- `comp_range` for BT scores against the $30 floor (≈$34 SFUSD comp = top marks).

### 14.3 Part-time / employment type
- Detect `employment_type` (part_time|full_time|contract|per_diem|unknown) from title/JD; prefer part-time for BT. Surface as a **"Job type"** filter facet (applies to both tracks).

### 14.4 Sources (D6)
- `config/target_companies.yaml`: add Bay Area ABA providers' careers pages (verify each board during build): Easterseals NorCal / Catalight, Autism Learning Partners, Centria Autism, Kadiant, Maxim Healthcare, Intercare Therapy, STAR of CA, and other Bay providers with public boards.
- New scanner `agents/scanner/edjoin.py`: query **EdJoin** for Bay Area BT / paraprofessional / behavior roles (SFUSD + nearby districts), normalize into `Job` rows like other scanners, wire into the scheduler sweep. Polite rate limits; respect ToS/robots.

### 14.5 BT-aware tailoring (D7)
- For `behavioral_technician` jobs, `agents/tailor.py` leads with the **BIA Behavioral Technician** experience + **BCAT** cert + data-collection/treatment-plan skills, de-emphasizing software/AI projects (archetype-conditional ordering). Still sourced from the inserted resume.
- Apply mode: supported ATS (Greenhouse/Lever/Ashby/Workday/generic) that clear $30/hr + score guardrails → auto-submit; unsupported portals (EdJoin/niche ABA ATS) → tailored materials + manual queue.

### 14.6 Dashboard
- BT roles are another **role category** chip in the faceted rail; add the **Job type** facet and show **hourly pay** ("$32/hr") on cards/detail when `pay_period = hourly`. Selecting the "Behavioral Technician" chip = view the BT track only.

### 14.7 QA additions
- E2E: a BT fixture flows through scoring with hourly pay; the $30/hr floor excludes a $24/hr role; "Job type = Part-time" filters correctly; a BT job on an unsupported ATS routes to the manual queue with tailored materials (not auto-submitted).
- Continuous loop rotates a BT keyword ("Behavioral Technician") through the same 6 steps.

### 14.8 Acceptance (BT)
- BT roles in SF/Bay surface, classified `behavioral_technician`, scored with the pay-weighted rubric.
- Roles < $30/hr excluded from auto-apply; ≥$30/hr (ideally ~$34) rank high.
- Supported-ATS BT roles auto-submit on passing guardrails; unsupported portals queue tailored materials for manual apply.
- Tailored BT résumé leads with behavioral experience, not AI projects.
