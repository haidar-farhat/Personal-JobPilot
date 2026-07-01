# AI-Role Targeting + Indeed UI + BT Track — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend JobPilot to target AI-engineering roles (+ an "AI-forward" signal) and part-time Behavioral Technician roles (≥$30/hr), re-skin the dashboard to Indeed's look with a full faceted filter rail, and verify everything via Playwright + a continuous QA loop.

**Architecture:** Same scan→score→tailor→apply→dashboard pipeline. Role targeting is mostly `config/archetypes.yaml` (ranker reads it dynamically). New scoring signal (`ai_intensity`) + hourly-comp fields are additive nullable DB columns (migrations 004, 005). UI is a rebuild of the single static `server/static/index.html`, reusing the existing JS data layer and SSE. BT sourcing adds an EdJoin scanner + ABA career pages.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy 2.0, SQLite, APScheduler, Ollama (gemma4:31b), Playwright (Python), pytest, vanilla HTML/CSS/JS.

## Global Constraints

- Archetype weight vectors MUST sum to 1.0 (ranker validates on load).
- All new `JobScore`/`Job` columns are additive + nullable; back up `jobpilot.db` before each migration.
- New code reads new fields via `getattr(obj, "field", None)` so pre-backfill rows render.
- Never weaken production guardrails. QA/apply-simulation uses `dry_run=True` explicitly; `applicant_profile.yaml guardrails.dry_run` stays `false`.
- BT auto-apply hard floor: never submit a BT role known to pay < $30/hr.
- Back up `server/static/index.html` to `index.html.bak-pre-indeed` before rewriting.
- Dashboard stays at `127.0.0.1:7777`; reuse existing endpoints (no breaking API changes; only additive fields).
- Spec of record: `docs/superpowers/specs/2026-06-29-ai-roles-indeed-ui-design.md`.

---

## Phase 1 — Archetypes, keywords, companies, resume sync (config)

### Task 1: Add AI + BT archetypes to `config/archetypes.yaml`
**Files:** Modify `config/archetypes.yaml`; Test `tests/test_archetype_ranker.py`.
**Interfaces:** Produces archetype keys `ai_engineer`, `ai_solutions_engineer`, `ai_analyst`, `behavioral_technician`; re-scoped `ml_engineer`. Each with `label`, `description`, `keywords`, `auto_apply_min_score`, `weights` (10 dims summing to 1.0). Values per spec §4.1 and §14.1.

- [ ] **Step 1 — failing test:** extend the existing weight-sum test to assert ALL archetypes (incl. the 4 new) sum to 1.0 and that the 4 new keys exist with non-empty `keywords` and an `auto_apply_min_score`.
```python
def test_all_archetypes_weights_sum_to_one():
    cfg = _load_archetypes_yaml()
    for key, arch in cfg["archetypes"].items():
        s = round(sum(arch["weights"].values()), 6)
        assert s == 1.0, f"{key} weights sum to {s}"

def test_new_archetypes_present():
    cfg = _load_archetypes_yaml()
    for key in ["ai_engineer","ai_solutions_engineer","ai_analyst","behavioral_technician"]:
        assert key in cfg["archetypes"], f"missing {key}"
        assert cfg["archetypes"][key]["keywords"]
        assert "auto_apply_min_score" in cfg["archetypes"][key]
```
- [ ] **Step 2 — run, expect FAIL** (`pytest tests/test_archetype_ranker.py -v`).
- [ ] **Step 3 — implement:** add the 4 archetypes (weights per spec §4.1/§14.1), re-scope `ml_engineer` keywords (drop `ai engineer`; keep production-ML terms).
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — verify ranker loads** (no weight-validation error): `python -c "from agents.ranker import _load_archetype_config as L; print(list(L()['archetypes']))"`.
- [ ] **Step 6 — commit:** `feat(ranker): add ai_engineer/ai_solutions_engineer/ai_analyst/behavioral_technician archetypes`.

### Task 2: Verify the classification prompt enumerates archetypes from YAML
**Files:** Read `agents/ranker.py` (`ARCHETYPE_PROMPT_TEMPLATE`, `classify_archetype`, ll. 103–160); Modify only if hardcoded.
- [ ] **Step 1:** Read the template; confirm it formats archetype keys+descriptions from `_load_archetype_config()`. If hardcoded, replace with a generated block. If already dynamic, no change.
- [ ] **Step 2 — sanity test:** unit test that the built prompt string contains "behavioral_technician" and "ai_engineer".
- [ ] **Step 3 — commit** if changed: `fix(ranker): build archetype list from config`.

### Task 3: Expand search keywords + AI-forward companies + BT sources
**Files:** Modify `config/settings.yaml` (`search.keywords`, new `comp.min_hourly_by_archetype`, new `ai_intensity.badge_threshold: 60`); Modify `config/target_companies.yaml` (AI cos + ABA cos).
- [ ] **Step 1:** append AI + BT keywords to `search.keywords` (spec §4.3, §14: AI Engineer, LLM Engineer, Solutions Engineer, Forward Deployed Engineer, AI Analyst, Prompt Engineer, Machine Learning Engineer, Behavioral Technician, Registered Behavior Technician, RBT, ABA Therapist).
- [ ] **Step 2:** add `comp:\n  min_hourly_by_archetype:\n    behavioral_technician: 30` and `ai_intensity:\n  badge_threshold: 60` to settings.yaml.
- [ ] **Step 3:** add AI-forward + ABA companies to `target_companies.yaml` (verify each board slug resolves during Phase 3 scanner work; comment any unverified).
- [ ] **Step 4 — verify YAML loads:** `python -c "import yaml; yaml.safe_load(open('config/settings.yaml')); yaml.safe_load(open('config/target_companies.yaml')); print('ok')"`.
- [ ] **Step 5 — commit:** `feat(config): AI+BT keywords, AI-forward companies, ABA sources, hourly floor`.

### Task 4: Sync `base_resume.yaml` + `applicant_profile.yaml` to the new resume
**Files:** Modify `config/base_resume.yaml`, `config/applicant_profile.yaml`.
- [ ] **Step 1:** base_resume — add TypeScript to Languages; add AWS/Vercel/Nuxt/Vue/Node/WordPress/Postgres to the web/tools line; add **Leasing Agent 415** project + **ForgetMeNote iOS** project; add **Freelance Software Engineer — Leasing Agent 415 (Compass)** work entry (lead); add **DataCamp AI Engineer for Developers Associate (2026)** + **DataCamp Data Analyst Associate (2026)** certs; extend `strong_match_keywords` (MCP, prompt engineering, RAG, agents, Claude API, OpenAI API, Ollama, full-stack, TypeScript, solutions engineering, forward deployed).
- [ ] **Step 2:** applicant_profile — set `links.github: https://github.com/TheCromazone`; add an AI-eng `fallback_essays` variant; leave guardrails/salary/work-auth unchanged.
- [ ] **Step 3 — verify load:** `python -c "import yaml; yaml.safe_load(open('config/base_resume.yaml')); yaml.safe_load(open('config/applicant_profile.yaml')); print('ok')"`.
- [ ] **Step 4 — commit:** `feat(config): sync resume + profile to AI-engineering resume`.

---

## Phase 2 — AI-intensity signal (migration 004 + ranker)

### Task 5: Add `ai_intensity` + `ai_tools` to `JobScore` + migration 004
**Files:** Modify `db/models.py` (JobScore); Create `db/migrations/004_add_ai_intensity.py`; Test `tests/test_migration_004.py`.
**Interfaces:** Produces `JobScore.ai_intensity: int|None`, `JobScore.ai_tools: list|None`.
- [ ] **Step 1 — failing test:** assert a `JobScore` can be created with `ai_intensity=72, ai_tools=["LLM APIs"]` and round-trips via the session.
- [ ] **Step 2 — run, expect FAIL** (column missing).
- [ ] **Step 3 — implement:** add `ai_intensity = Column(Integer, nullable=True)` and `ai_tools = Column(JSON, nullable=True)` to `JobScore`; write migration 004 mirroring 003's style (ALTER TABLE add columns; idempotent guard).
- [ ] **Step 4 — back up DB + run migration:** copy `jobpilot.db`→`jobpilot.db.bak-004`; `python -m db.migrations.004_add_ai_intensity`.
- [ ] **Step 5 — run test, expect PASS.**
- [ ] **Step 6 — commit:** `feat(db): add ai_intensity + ai_tools to JobScore (migration 004)`.

### Task 6: Ranker emits `ai_intensity` + `ai_tools`
**Files:** Modify `agents/ranker.py` (scoring stage + persistence); Test `tests/test_archetype_ranker.py`.
**Interfaces:** Consumes archetype config. Produces `ai_intensity` (0–100), `ai_tools` (list) persisted on JobScore.
- [ ] **Step 1 — failing test:** with a mocked `generate_json` returning an `ai_intensity`, assert the ranker stores it on the JobScore.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** extend the stage-2 (dimension) prompt/schema to also return `ai_intensity` (how central is building-with/using AI tooling, 0–100) + `ai_tools` (list); persist both. Default to `None`/`[]` if the model omits them.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(ranker): emit ai_intensity + ai_tools`.

### Task 7: Serialize new fields for the dashboard
**Files:** Modify `server/dashboard.py` (`_serialize_application`, ll. 97–142).
- [ ] **Step 1 — failing test (`tests/e2e/test_api_contracts.py`):** assert `/api/applications` items include keys `ai_intensity` and `ai_tools`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** add `"ai_intensity": getattr(score,"ai_intensity",None) if score else None` and `"ai_tools": getattr(score,"ai_tools",None) if score else []`.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(api): expose ai_intensity + ai_tools`.

---

## Phase 3 — BT track: hourly comp, employment type, sources, tailoring

### Task 8: `Job` hourly-comp + employment_type fields + migration 005
**Files:** Modify `db/models.py` (Job); Create `db/migrations/005_add_hourly_and_emptype.py`; Create `utils/comp.py` (pure parser); Test `tests/test_comp_parser.py`.
**Interfaces:** Produces `Job.pay_period: str|None`, `Job.hourly_min: float|None`, `Job.hourly_max: float|None`, `Job.employment_type: str|None`; `utils/comp.py::parse_hourly(text)->(min,max)|None`, `parse_employment_type(title, desc)->str`.
- [ ] **Step 1 — failing test:** `parse_hourly("$28–$35 an hour")==(28.0,35.0)`, `parse_hourly("$30/hr")==(30.0,30.0)`, `parse_hourly("$120,000/yr") is None`; `parse_employment_type("Part-Time Behavior Technician","")=="part_time"`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** `utils/comp.py` (regex for `$N`, `$N-$M`, `/hr|per hour|hourly|an hour`; year detection returns None for hourly); add the 4 nullable columns; migration 005 (back up DB first).
- [ ] **Step 4 — run migration + tests, expect PASS.**
- [ ] **Step 5 — commit:** `feat(db): hourly comp + employment_type on Job (migration 005) + comp parser`.

### Task 9: Populate comp/emptype at scan time + BT comp scoring + auto-apply floor
**Files:** Modify `agents/scanner/base.py` (set `pay_period/hourly_*` + `employment_type` from parser when ingesting); Modify `agents/ranker.py` (BT `comp_range` scores vs `min_hourly_by_archetype`); Modify `agents/auto_applier/runner.py` (skip BT < $30/hr); Tests in `tests/test_auto_applier_units.py`, `tests/test_archetype_ranker.py`.
**Interfaces:** Consumes `utils/comp.py`, `settings.yaml comp.min_hourly_by_archetype`.
- [ ] **Step 1 — failing tests:** runner excludes a `behavioral_technician` app with `hourly_max=24` from eligibility; includes one with `hourly_min=32`.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** scanner populates fields; runner eligibility adds the BT hourly floor; ranker comp_range uses the floor for BT.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(bt): hourly floor in auto-apply + comp scoring + scan-time population`.

### Task 10: EdJoin scanner
**Files:** Create `agents/scanner/edjoin.py`; Modify `scheduler.py` (register in sweep); Test `tests/test_edjoin_scanner.py` (parse a saved fixture, no live net in unit test).
**Interfaces:** Produces `EdJoinScanner.scan()->list[JobDict]` matching the other scanners' output shape (title, company/district, location, url, description, source="edjoin").
- [ ] **Step 1 — failing test:** feed a saved EdJoin results HTML/JSON fixture to the parser; assert it yields normalized job dicts with `source=="edjoin"` and a Bay Area location.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement:** query EdJoin for Bay Area BT/paraprofessional/behavior roles; polite rate limit + timeout + graceful failure (log, return []); wire into scheduler sweep.
- [ ] **Step 4 — run unit test, expect PASS; then one guarded live smoke** (`python -m agents.scanner.edjoin` prints count; tolerate 0/blocked).
- [ ] **Step 5 — commit:** `feat(scanner): EdJoin scanner for school-district BT roles`.

### Task 11: BT-aware tailoring
**Files:** Modify `agents/tailor.py` (archetype-conditional section ordering); Test `tests/test_tailor_bt.py`.
**Interfaces:** Consumes archetype; for `behavioral_technician`, leads with BIA experience + BCAT + behavioral skills.
- [ ] **Step 1 — failing test:** for a BT job, the assembled resume context lists the BIA Behavioral Technician entry before the software/AI projects.
- [ ] **Step 2 — run, expect FAIL.**
- [ ] **Step 3 — implement** archetype-conditional ordering/emphasis.
- [ ] **Step 4 — run, expect PASS.**
- [ ] **Step 5 — commit:** `feat(tailor): BT-aware ordering (lead with behavioral experience)`.

---

## Phase 4 — Backfill re-score

### Task 12: `scripts/rescore_archetypes.py` + run
**Files:** Create `scripts/rescore_archetypes.py`; (no new tests beyond a `--limit 2` dry run).
**Interfaces:** Re-runs `classify_archetype` + dimension scoring (+ ai_intensity) for existing Jobs; idempotent; flags `--limit N`, `--ids`.
- [ ] **Step 1 — implement** the script (transaction per job, progress log, resumable).
- [ ] **Step 2 — back up DB** → `jobpilot.db.bak-rescore`.
- [ ] **Step 3 — smoke:** `python scripts/rescore_archetypes.py --limit 2` → verify 2 rows updated, archetypes/ai_intensity populated.
- [ ] **Step 4 — full run:** `python scripts/rescore_archetypes.py` (watch progress; ~minutes).
- [ ] **Step 5 — verify:** query count of jobs now classified `ai_engineer`/`behavioral_technician`; spot-check `ai_intensity` populated.
- [ ] **Step 6 — commit:** `feat(scripts): archetype + ai_intensity backfill re-score`.

---

## Phase 5 — Indeed-style UI rebuild + faceted filters

### Task 13: Back up + scaffold Indeed shell
**Files:** Copy `server/static/index.html`→`index.html.bak-pre-indeed`; Modify `server/static/index.html`.
- [ ] **Step 1:** back up current index.html.
- [ ] **Step 2:** implement the Indeed shell per spec §7 — design tokens (Noto Sans, `#2557A7`, etc.), header w/ health dot, two-field What/Where search bar, left filter-rail container, results-list column, sticky detail-pane column, responsive collapse. Keep the existing `<script>` data layer functions (fetchJSON, SSE wiring, renderers) — re-target their output into the new DOM containers; preserve Insights as a tab.
- [ ] **Step 3 — verify served:** `curl -s 127.0.0.1:7777/ | findstr /C:"Find"` shows search button; page is 200.
- [ ] **Step 4 — commit:** `feat(ui): Indeed-style shell (search + rail + list/detail)`.

### Task 14: Job cards + detail pane
**Files:** Modify `server/static/index.html`.
- [ ] **Step 1:** render each application as an Indeed-style card (title/company/location/salary or `$X/hr`/fit chip/archetype badge/⚡AI-forward badge if `ai_intensity>=threshold`/status pill/"Posted Nd ago"). Selected card highlights.
- [ ] **Step 2:** detail pane renders the selected job: header + apply/open-link + résumé/cover buttons + status control + 10-dim score grid + collapsible evaluation (reuse `/api/application/{id}/evaluation`).
- [ ] **Step 3 — verify:** load page, confirm cards + clicking a card fills the detail pane (manual curl of evaluation endpoint for one id).
- [ ] **Step 4 — commit:** `feat(ui): job cards + detail pane with AI-forward + hourly pay`.

### Task 15: Faceted filter rail (client-side)
**Files:** Modify `server/static/index.html`.
**Interfaces:** Pure client-side predicate over loaded `/api/applications`.
- [ ] **Step 1:** build facets per spec §8 — Role category, Fit tier (≥75/60–74/<60), AI-forward toggle, Location/Remote, Date posted (24h/3d/7d/14d), Source, Status, Job type (part_time/…), Min-score slider. Multi-select OR within facet, AND across; live counts; Clear all; reflect to URL query.
- [ ] **Step 2:** wire What/Where inputs into the same predicate.
- [ ] **Step 3 — verify:** load page; apply a couple facets; confirm count updates and list narrows.
- [ ] **Step 4 — commit:** `feat(ui): full faceted filter rail`.

---

## Phase 6 — Playwright E2E

### Task 16: Extend `tests/e2e/` + ensure chromium
**Files:** Create `tests/e2e/test_indeed_ui.py`; Modify `tests/e2e/conftest.py` if needed; Test data via existing fixtures/DB.
- [ ] **Step 1:** `playwright install chromium` (venv).
- [ ] **Step 2 — write tests** (per spec §10.1, §14.7): `test_search_what_where`, `test_facets` (incl. no-false-positives + BT $30/hr floor excludes $24/hr fixture + Job-type filter), `test_list_detail`, `test_ai_forward`, `test_apply_simulation` (drives `dry_run=True`; asserts reaches `dry_run_skip_submit`, never a real Submit).
- [ ] **Step 3 — run:** `python -m pytest tests/e2e/ -v` against the running dashboard; capture screenshots into `tests/qa-screenshots/`.
- [ ] **Step 4 — fix until green**; keep prior API/SSE/render tests passing.
- [ ] **Step 5 — commit:** `test(e2e): Indeed UI, facets, AI-forward, BT floor, dry-run apply sim`.

---

## Phase 7 — Continuous QA loop

### Task 17: Loop harness + run
**Files:** Create `scripts/qa_loop.py` (optional helper) or drive via the loop skill.
- [ ] **Step 1:** confirm full E2E green + dashboard healthy.
- [ ] **Step 2:** engage the loop (loop skill, self-paced) implementing spec §10.2 steps 1–6, emitting exactly:
  `[Loop #] | Status: SUCCESS/FAILED | Search Term: X | Jobs Scanned: N | Filtered: M | App Simulation: PASS/FAIL | Error Log: None or <error>`
- [ ] **Step 3:** rotate keywords incl. "AI Engineer", "Solutions Engineer", "Behavioral Technician", "Data Analyst"; dry-run only; randomized 5–10s cooldown.
- [ ] **Step 4:** report steady SUCCESS lines; stop on user request.

---

## Self-Review

- **Spec coverage:** §3 D1–D7 → Tasks 1,3,5–11 (archetypes, ai_intensity, BT comp/sources/tailoring, apply mode). §4 → T1–T3. §5 → T5–T7. §6 → T4. §7 → T13–T14. §8 → T15. §9 backfill → T12. §10 QA → T16–T17. §14 → T1,T3,T8–T11,T16. All covered.
- **Placeholder scan:** UI tasks reference spec §7/§8 tokens+layout rather than inlining ~1000 lines of HTML — intentional for a large existing single-file frontend; all behavioral requirements are explicit.
- **Type consistency:** `ai_intensity`/`ai_tools` names consistent across model/migration/ranker/serializer/UI; `pay_period/hourly_min/hourly_max/employment_type` consistent across model/parser/scanner/runner/UI; `parse_hourly`/`parse_employment_type` defined in `utils/comp.py` (T8), consumed in T9.

## Execution notes
- Verify each phase against the **running** dashboard (already live at :7777) before moving on. The scheduler keeps running; migrations + backfill only add columns / update `JobScore`.
- Restart the dashboard (or rely on watchdog) after serializer changes so the API serves new fields.
